import { html, useEffect, useLayoutEffect, useMemo, useRef, useState } from '../lib.js'
import { Check, ChevronDown, ChevronRight, CircleAlert, Copy, FileText, GitBranch, LoaderCircle, Pencil, RefreshCw, Terminal, Undo2, X } from '../icons.js'
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
import { backupTitle, setRewindNotice } from '../rewindNotice.js'
import { agentLabel, agentSettingsText, elapsedText } from '../subagents.js'
import { pathShortener } from '../paths.js'
import { hintText } from '../mcp.js'

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

// A command that starts with "cd <folder> &&" (one plain or quoted absolute folder, no shell expansion and no
// operators inside it) as [folder, rest]; null otherwise. Only spaces or tabs between cd, the folder and "&&": a line
// break there ends the cd, and the shell then rejects or runs on its own what follows.
const LEADING_CD = /^\s*cd[ \t]+(?:'([^'\n&;|<>]+)'|"([^"\n$`\\&;|<>]+)"|([^\s'"$`\\;&|<>(){}*?[\]~]+))[ \t]*&&\s*(\S[\s\S]*)$/
function leadingCd(command) {
  const match = LEADING_CD.exec(command)
  const folder = match && (match[1] ?? match[2] ?? match[3])
  if (!folder?.startsWith('/') || folder.split('/').some(part => part === '.' || part === '..')) return null
  return [folder, match[4]]
}

// The collapsed heading of a command card: "cd <folder> && rest" reads "<folder name> › rest" when the folder is the
// project or inside a known folder, so the command itself is not cut off. A command run with cwd reads
// "<folder> › command" the same way (cwd: the resolved folder, else as the agent gave it). Display only: the title,
// the accessible name, the expanded card and the approval card keep the whole command.
export function commandHeading(command, shorten, projectName = '', cwd = '') {
  if (typeof cwd === 'string' && cwd) {
    const short = shorten(cwd)
    if (short !== '.') return `${short} › ${command}`
  }
  const cd = leadingCd(command)
  const short = cd ? shorten(cd[0]) : null
  if (!cd || short === cd[0]) return command
  return `${short === '.' ? projectName || '.' : short} › ${cd[1]}`
}

// Returns Map<event id, { subject, shown, taskStatus? }> for every tool event. subject is the full text (the title
// and accessible name); shown is what the heading shows: a path inside the project relative to it, one in an
// allowed folder as "<folder name>/rest" (shorten: pathShortener), and a leading cd into such a folder shortened.
function summarizeTools(events, shorten = path => path, projectName = '') {
  const taskTitles = new Map()
  const summaries = new Map()
  const flat = text => text.slice(0, 240).replace(/\s+/g, ' ')
  for (const event of events) {
    if (event.type !== 'tool') continue
    const input = event.input || {}
    let subject = String(input.path || input.file_path || input.command || input.query || input.task || input.title || '')
    let shown = PATH_SUBJECTS.has(event.name) && input.path && typeof input.path === 'string' ? shorten(input.path)
      : event.name === 'run_command' && typeof input.command === 'string' ? commandHeading(input.command, shorten, projectName, event.command_cwd || input.cwd) : subject
    let suffix = ''
    if (event.name === 'read_file') suffix = readRange(event)
    if (event.name === 'find_files') {
      const where = input.path && input.path !== '.' ? input.path : ''
      subject = `${input.pattern ?? ''}` + (where ? ` in ${where}` : '')
      shown = `${input.pattern ?? ''}` + (where ? ` in ${shorten(where)}` : '')
    }
    if (event.name === 'remember') subject = shown = String(input.text ?? '')
    if (event.name === 'wait_job') subject = shown = String(input.job_id ?? '').slice(0, 8) + (input.until_regex ? ` until /${input.until_regex}/` : '')
    if (event.name === 'outline_file') {
      suffix = (Number.isInteger(input.max_level) && input.max_level < 6 ? ` · to level ${input.max_level}` : '')
        + (Number.isInteger(input.offset) && input.offset > 0 ? ` · from entry ${input.offset}` : '')
    }
    if (event.name === 'project_overview') {
      const where = input.path && input.path !== '.' ? String(input.path) : ''
      subject = where || 'workspace'
      shown = where && shorten(where) !== '.' ? shorten(where) : 'workspace'
    }
    subject += suffix
    shown += suffix
    // A hook card names its hook and the tool it ran for (lifecycle events have no tool).
    if (String(event.name || '').startsWith('hook_') && input.hook_id) subject = shown = `${input.hook_id}${input.tool ? ` · ${input.tool}` : ''}`
    let taskStatus
    if (event.name === 'create_task' || event.name === 'update_task') {
      let task = {}
      try {
        const result = JSON.parse(event.output || '{}')
        if (result && typeof result === 'object' && !Array.isArray(result)) task = result
      } catch { /* Older or failed events can contain plain-text output. */ }
      subject = shown = String(task.title || input.title || taskTitles.get(String(input.task_id)) || input.task_id || '')
      if (event.state === 'completed') {
        if (typeof task.id === 'string' && subject) taskTitles.set(task.id, subject)
        const status = task.status || input.status
        if (typeof status === 'string') taskStatus = status.replace(/_/g, ' ')
      }
    }
    summaries.set(event.id, { subject: flat(subject), shown: flat(shown), taskStatus })
  }
  return summaries
}

// Results the UI can read: a command or job page ({ job_id, state, exit_code, output, … }, after the edited-command
// note when there is one) and an MCP result ({ output, is_error, truncated }).
const JOB_TOOLS = new Set(['run_command', 'user_command', 'wait_job', 'get_job_output', 'stop_job'])

// Which part of the output a job page holds, or null when it holds all of it.
function shownPart(page) {
  if (page.excerpt) return 'start and end shown; the agent can read the omitted middle with get_job_output'
  if ('output_start' in page) {  // wait_job: the end of the output
    // Characters are counted from 1, as in the get_job_output range below.
    return Number(page.output_start) > 0 ? `latest output shown, from character ${(Number(page.output_start) + 1).toLocaleString()}` : null
  }
  const offset = Number(page.offset ?? 0)
  // The backend's own counts (code points): JS string lengths count UTF-16 units.
  const end = Number(page.next_offset ?? page.total_chars ?? offset + page.output.length)
  if (offset === 0 && page.next_offset != null && page.next_offset < page.total_chars) return 'first part shown; the agent can read the rest with get_job_output'
  if (offset > 0 && end <= offset) return `no output after character ${offset.toLocaleString()}`
  if (offset > 0) return `characters ${(offset + 1).toLocaleString()}–${end.toLocaleString()} shown`
  return null
}

// { note, text, raw, mcpError, job: { state, exitCode, background, total, truncated, shown } | null } for a finished
// command, job or MCP card whose output parses; null otherwise (older events, plain-text errors). raw is the whole
// result the agent received (the edited-command note and the page).
export function resultOf(event) {
  const name = event.name || ''
  if (!(JOB_TOOLS.has(name) || name.startsWith('mcp__')) || event.state === 'running') return null
  const text = String(event.output || '')
  let note = null
  let json = text
  if (text.startsWith('[')) {
    // The note quotes the edited command, which can contain '\n{'; the page is indented JSON whose only
    // line starting with '{' is its first, so the last '\n{' starts the page.
    const at = text.lastIndexOf('\n{')
    if (at < 0) return null
    note = text.slice(0, at)
    json = text.slice(at + 1)
  }
  let page
  try { page = JSON.parse(json) } catch { return null }
  if (!page || typeof page !== 'object' || Array.isArray(page) || typeof page.output !== 'string') return null
  return { note, text: page.output, raw: text, mcpError: page.is_error === true,
    // A long MCP result: the rest is kept in memory for get_tool_output (result_store.py).
    kept: typeof page.result_id === 'string' && Number.isInteger(page.total_chars)
      ? { shown: Number(page.next_offset) || page.output.length, total: page.total_chars, restNotKept: page.rest_not_kept === true } : null,
    job: 'state' in page ? { state: String(page.state), exitCode: page.exit_code, background: !!page.background,
      total: page.total_chars, truncated: page.truncated === true, shown: shownPart(page),
      // wait_job: why the wait ended ('matched', 'timeout', 'idle' or 'exited') and the line that matched.
      wait: typeof page.reason === 'string' ? page.reason : null, match: typeof page.match?.line === 'string' ? page.match.line : null } : null }
}

const KIND_LABEL = { timeout: 'timed out', not_found: 'not found', edit_mismatch: 'no match', stale_file: 'file changed',
  excluded: 'excluded', limit: 'too large', command_refused: 'refused', access_required: 'needs access',
  invalid_arguments: 'invalid arguments', wrong_type: 'wrong type', permission_denied: 'permission denied',
  guidance_changed: 'instructions changed', unavailable_tool: 'unavailable', encoding: 'encoding' }

// What a failed tool told the agent ({ kind, label, hint }), from its model_output; null when unknown.
function toolError(event) {
  if (event.state !== 'error' || typeof event.model_output !== 'string') return null
  let parsed
  try { parsed = JSON.parse(event.model_output) } catch { return null }
  const kind = parsed?.error_kind
  if (typeof kind !== 'string' || !kind) return null
  return { kind, label: KIND_LABEL[kind] || kind.replace(/_/g, ' '), hint: typeof parsed.hint === 'string' ? parsed.hint : null }
}

// A negative exit code is the signal that ended the process (-15: SIGTERM).
const exitLabel = code => !Number.isInteger(code) ? 'failed' : code < 0 ? `signal ${-code}` : `exit ${code}`
// The heading label of a command's own job (run_command, user_command): [tone, label, labelTone].
function commandJob(job) {
  if (!job) return null
  if (job.state === 'failed') return ['warn', exitLabel(job.exitCode), 'warn']
  if (job.state === 'timed_out') return ['warn', 'timed out', 'warn']
  if (job.state === 'interrupted') return ['warn', 'interrupted', 'warn']
  if (job.state === 'cancelled') return ['neutral', 'stopped', 'neutral']
  if (job.state === 'running' && job.background) return ['neutral', 'started in background', 'neutral']
  return null
}
// The label of the job a wait_job, get_job_output or stop_job card reports on.
const OTHER_JOB = { timed_out: 'job timed out', running: 'job running', cancelled: 'job stopped', interrupted: 'job interrupted' }

// The card's status: tone (the icon: 'running', 'ok', 'warn', 'neutral' or null), label and labelTone (its colour:
// 'warn' or 'neutral', set with every label). A job tool can work (ok) while its job failed (a neutral label).
export function toolStatus(event, result) {
  const none = { tone: null, label: '', labelTone: null }
  if (event.state === 'running') return { tone: 'running', label: '', labelTone: null }
  const isCommand = event.name === 'run_command' || event.name === 'user_command'
  const own = isCommand ? commandJob(result?.job) : null
  if (event.state === 'error') {
    const label = own?.[1] || (result?.mcpError ? 'tool error' : toolError(event)?.label || 'failed')
    return { tone: 'warn', label, labelTone: 'warn' }
  }
  if (event.state !== 'completed') return none
  if (own) return { tone: own[0], label: own[1], labelTone: own[2] }
  const job = result?.job
  if (job && !isCommand && JOB_TOOLS.has(event.name)) {
    // A wait reports its outcome first: its line matched, or it timed out (no tick: what it waited for did not come).
    if (job.wait === 'matched') return { tone: 'ok', label: 'matched', labelTone: 'neutral' }
    if (job.wait === 'timeout') return { tone: 'neutral', label: 'wait timed out', labelTone: 'neutral' }
    if (job.wait === 'idle' && job.state === 'running') return { tone: 'neutral', label: 'output quiet', labelTone: 'neutral' }
    const label = job.state === 'failed' ? `job ${exitLabel(job.exitCode)}` : OTHER_JOB[job.state] || (job.wait === 'exited' ? 'job ended' : '')
    return { tone: 'ok', label, labelTone: label ? 'neutral' : null }
  }
  return { tone: 'ok', label: '', labelTone: null }
}

// The Action word: a command that ran and then failed says 'ran', not 'completed' beside its failed state.
const actionText = (event, status) => event.state === 'completed' && status?.tone === 'warn' ? 'ran' : event.state

// Parsed results, statuses and errors are cached per event object: an updated event is a new object.
const statusCache = new WeakMap()
function toolState(event) {
  let state = statusCache.get(event)
  if (!state) {
    const result = resultOf(event)
    state = { result, status: toolStatus(event, result), failure: toolError(event) }
    statusCache.set(event, state)
  }
  return state
}

// The State line of an expanded command or job card.
const KILLED = new Set(['timed_out', 'cancelled', 'interrupted'])  // the app ended the process: its code says nothing more
const WAIT_LINE = { matched: 'wait matched', timeout: 'wait timed out', idle: 'output went quiet', exited: 'job ended during the wait' }
function jobStateLine(job) {
  const wait = WAIT_LINE[job.wait] ? WAIT_LINE[job.wait] + (job.wait === 'matched' && job.match ? `: ${clip(job.match, 80)}` : '') : ''
  return [`State: ${job.state.replace(/_/g, ' ')}`, wait, Number.isInteger(job.exitCode) && !KILLED.has(job.state) ? exitLabel(job.exitCode) : '',
    typeof job.total === 'number' ? `${job.total.toLocaleString()} characters` : '',
    job.truncated && typeof job.total === 'number' ? `output after ${job.total.toLocaleString()} characters not kept` : '',
    job.shown || ''].filter(Boolean).join(' · ')
}

// Hooks (lifecycle_hooks.py). A hook card's structured result is hook_result ({ decision?, reason?, context?,
// arguments?: { before, after }, prompt?: { before, after }, report?, ignored? }); a tool card can carry hook_change
// ({ hooks, original }: a before-tool hook replaced the arguments), hook_ask ({ hook_id, reason }: a hook asked for
// its card) and hook_blocked ({ hook_id, reason }: a hook's deny).
const STOP_HOOKS = new Set(['hook_agent_stop', 'hook_subagent_stop'])
export function hookSummary(event) {
  const result = event.hook_result
  const parts = []
  if (result && typeof result === 'object') {
    const reason = clip(result.reason, 160)
    if (result.decision === 'deny') parts.push(`Hook blocked: ${reason}`)
    else if (result.decision === 'block') parts.push(STOP_HOOKS.has(event.name) ? `Hook asked the agent to continue: ${reason}` : `Hook blocked the message: ${reason}`)
    else if (result.decision === 'ask') parts.push(`Hook asked for approval: ${reason}`)
    if (result.arguments) parts.push('Hook changed the arguments')
    if (result.prompt) parts.push('Hook edited the message')
    if (result.report) parts.push('Hook replaced the report')
    if (result.context) parts.push('Hook added context')
  }
  const hooks = Array.isArray(event.hook_change?.hooks) ? event.hook_change.hooks.join(', ') : ''
  if (event.hook_change) parts.push(hooks ? `Hook ${hooks} changed the arguments` : 'A hook changed the arguments')
  if (event.hook_ask && event.state !== 'pending') parts.push(`Hook ${event.hook_ask.hook_id} asked for approval`)
  return parts.join(' · ')
}

const pretty = value => typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)

function HookResult({ result }) {
  if (!result || typeof result !== 'object') return null
  return html`<div class="hook-result">
    ${result.reason ? html`<p class="tool-detail-label">Reason</p><p class="hook-reason">${result.reason}</p>` : null}
    ${result.context ? html`<p class="tool-detail-label">Context added for the agent</p><pre aria-label="Hook context">${result.context}</pre>` : null}
    ${result.arguments ? html`<p class="tool-detail-label">Arguments before the hook</p><pre aria-label="Arguments before">${pretty(result.arguments.before)}</pre>
      <p class="tool-detail-label">Arguments after the hook</p><pre aria-label="Arguments after">${pretty(result.arguments.after)}</pre>` : null}
    ${result.prompt ? html`<p class="tool-detail-label">Message before the hook</p><pre aria-label="Message before">${result.prompt.before}</pre>
      <p class="tool-detail-label">Message after the hook</p><pre aria-label="Message after">${result.prompt.after}</pre>` : null}
    ${result.report ? html`<p class="tool-detail-label">Report the parent chat receives</p><pre aria-label="Replacement report">${result.report}</pre>` : null}
    ${Array.isArray(result.ignored) && result.ignored.length ? html`<p class="muted hook-ignored">Ignored fields: ${result.ignored.join(', ')}</p>` : null}
  </div>`
}

// Images an MCP tool returned (event.mcp_images: { id, mime_type, bytes }), kept in memory by the app and
// never sent to the model; each loads when asked.
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])
function McpImages({ images, sessionId }) {
  const [shown, setShown] = useState({})
  const load = async image => {
    setShown(current => ({ ...current, [image.id]: { loading: true } }))
    try {
      const data = await api(`/sessions/${encodeURIComponent(sessionId)}/tool-images/${encodeURIComponent(image.id)}`)
      setShown(current => ({ ...current, [image.id]: IMAGE_TYPES.has(data.mime_type) && typeof data.data === 'string'
        ? { src: `data:${data.mime_type};base64,${data.data}` } : { error: 'This image cannot be shown.' } }))
    } catch (e) { setShown(current => ({ ...current, [image.id]: { error: e.message } })) }
  }
  return html`<div class="mcp-images"><p class="tool-detail-label">Images (shown here, not sent to the model)</p>
    ${images.map((image, index) => {
      const state = shown[image.id] || {}
      const label = `image ${index + 1} (${image.mime_type}, ${Math.max(1, Math.round(image.bytes / 1024)).toLocaleString()} KB)`
      return html`<div class="mcp-image" key=${image.id}>
        ${state.src ? html`<img src=${state.src} alt=${`MCP tool ${label}`} />`
          : html`<button type="button" class="outline" disabled=${!!state.loading} onClick=${() => load(image)}>Show ${label}</button>`}
        ${state.error ? html`<p class="form-error">${state.error}</p>` : null}
      </div>`
    })}</div>`
}

// failure: what a failed tool told the agent (toolError), or null.
function ToolDetails({ event, pending, result = null, failure = null }) {
  const { command, ...options } = event.input || {}
  const hasCommand = typeof command === 'string'
  const job = result?.job
  return html`
    ${event.hook_change ? html`<p class="tool-detail-label">Before the hook (the agent's proposal)</p>
      <pre aria-label="Arguments before the hook">${pretty(event.hook_change.original)}</pre>` : null}
    <p class="tool-detail-label">${hasCommand ? 'Command' : 'Input'}${event.hook_change ? ' after the hook' : ''}</p>
    <pre aria-label=${hasCommand ? 'Command' : 'Input'}>${hasCommand ? (pending && event.preview ? event.preview : command) : JSON.stringify(event.input || {}, null, 2)}</pre>
    ${hasCommand && Object.keys(options).length && !(pending && event.preview) ? html`<p class="tool-detail-label">Options</p><pre aria-label="Options">${JSON.stringify(options, null, 2)}</pre>` : null}
    ${pending && !hasCommand && event.preview ? html`<p class="tool-detail-label">Approval preview</p><pre aria-label="Approval preview">${event.preview}</pre>` : null}
    ${result ? html`${result.note && !event.edited ? html`<p class="tool-rule">${result.note}</p>` : null}
        <p class="tool-detail-label">Output</p><pre aria-label="Output">${result.text || 'No output.'}</pre>
        ${job ? html`<p class="muted tool-job-state">${jobStateLine(job)}</p>` : null}
        ${result.kept ? html`<p class="muted tool-job-state">First ${result.kept.shown.toLocaleString()} of ${result.kept.total.toLocaleString()} characters shown${result.kept.restNotKept ? ' (the server sent more; the rest was not kept)' : ''}. The agent can read the rest with get_tool_output until the app restarts.</p>` : null}
        <details class="tool-raw"><summary>Raw result</summary><pre aria-label="Raw result">${result.raw}</pre></details>`
      : !pending || event.output ? html`<p class="tool-detail-label">Output</p><pre aria-label="Output">${event.output || (event.state === 'running' ? 'Waiting for output…' : 'No output.')}</pre>` : null}
    ${failure ? html`<p class="tool-detail-label">Error category</p><p>${failure.label}</p>
      ${failure.hint ? html`<p>Hint given to the agent: ${failure.hint}</p>` : null}` : null}
    <${HookResult} result=${event.hook_result} />`
}

const clip = (text, limit = 240) => {
  const flat = String(text || '').trim().replace(/\s+/g, ' ')
  return flat.slice(0, limit) + (flat.length > limit ? '…' : '')
}

// The collapsed line of a failed command or tool whose result parses: "Exit 1 · <last output line>".
const JOB_OUTCOME = { failed: job => (label => label[0].toUpperCase() + label.slice(1))(exitLabel(job.exitCode)),
  timed_out: () => 'Timed out', interrupted: () => 'Interrupted', cancelled: () => 'Stopped' }
// The note first_page adds when output past max_output_bytes was not kept (agents.py): it is addressed to the agent.
const NOT_KEPT = '[… output after this point was not kept'
function failureOutcome(event, result, status) {
  const job = result.job
  const lines = result.text.split('\n').map(line => line.trim()).filter(line => line && !line.startsWith(NOT_KEPT))
  // When the end of the output was not kept, the last kept line is not the command's last line.
  const lastLine = job?.truncated && typeof job.total === 'number'
    ? `output after ${job.total.toLocaleString()} characters was not kept` : lines.at(-1) || ''
  if (job && JOB_OUTCOME[job.state] && (status.tone === 'warn' || event.state === 'error')) {
    return clip([JOB_OUTCOME[job.state](job), lastLine].filter(Boolean).join(' · '))
  }
  return event.state === 'error' ? clip(result.text) || 'Tool returned an error' : null
}

// A Databricks error in plain words: { summary, details } (details: the full text, or null when it is the summary).
const LEADS = {
  authentication: () => 'Databricks did not accept your credentials (HTTP 401). Check the token in your credential file.',
  permission: () => 'Databricks denied access to this model (HTTP 403).',
  rate_limit: () => 'Databricks rate limited this request.',
  server: event => `Databricks had a server problem (HTTP ${event.http_status ?? 500}). Try again in a moment.`,
  invalid_request: event => `Databricks rejected the request (HTTP ${event.http_status ?? 400}).`,
  network: () => 'Could not reach Databricks. Check your network or VPN connection.',
}
// The advice agents.py (itpm_advice) appends to a final input-tokens-per-minute 429, also after a body that is not JSON.
const RATE_ADVICE = / (Each agent step resends the whole conversation|For about five steps a minute)/
// The provider's message ends a sentence before the app's own text that follows it.
const sentence = value => /[.!?…]$/.test(value) ? value : `${value}.`
export function errorSummary(event) {
  const text = String(event.text || '')
  const kind = event.error_kind
  const http = text.startsWith('Databricks returned HTTP')
  // The provider's own message from a JSON error body, for HTTP errors and errors without a kind
  // (compaction failures); other kinds are the app's own sentences and pass through.
  let message = null
  let cleaned = null
  let after = ''  // the app's own text after the body (a rate-limit error's advice, a compaction note)
  if (http || !kind || LEADS[kind]) {
    const source = text.replace(' [Error response truncated.]', '')
    const start = source.indexOf('{')
    const end = source.lastIndexOf('}')
    if (start >= 0 && end > start) {
      let body = null
      try { body = JSON.parse(source.slice(start, end + 1)) } catch { /* Not JSON, or cut off. */ }
      message = [body?.message, body?.error?.message, body?.error, body?.detail].find(value => typeof value === 'string' && value.trim()) ?? null
      if (message) {
        after = source.slice(end + 1).trim()
        cleaned = `${source.slice(0, start).trim().replace(/:\s*$/, '.')} ${after ? `${sentence(clip(message, 300))} ${after}` : clip(message, 300)}`.trim()
      }
    }
    if (!after && kind === 'rate_limit' && RATE_ADVICE.test(text)) after = text.slice(text.search(RATE_ADVICE)).trim()
  }
  let summary
  if ((http || kind === 'network') && LEADS[kind]) summary = [LEADS[kind](event), message && (after ? sentence(clip(message, 300)) : clip(message, 300)), after].filter(Boolean).join(' ')
  else if (cleaned) summary = cleaned
  else summary = text.length > 400 ? clip(text, 300) : text
  return { summary, details: text !== summary ? text : null }
}
// Error notices are summarised once per event object (the transcript renders on every streamed delta).
const summaryCache = new WeakMap()
function noticeError(event) {
  let error = summaryCache.get(event)
  if (!error) summaryCache.set(event, error = errorSummary(event))
  return error
}

// A wait notice saved by an older version still asks for Stop; once the chat is idle there is nothing to stop.
const idleNotice = text => typeof text === 'string' ? text.replace(/ Press Stop to cancel\.$/, '') : text

// A Stop leaves a notice that names what it interrupted (older notices have no stop_phase).
const STOPPED_TEXT = 'Stopped. You can continue this conversation.'
const STOP_TEXT = { streaming: 'Stopped while replying', tools: 'Stopped during an action', waiting: 'Stopped' }

function toolOutcome(event) {
  const clipped = clip(event.output)
  if (event.state === 'rejected') {
    // A before-tool hook's deny: its hook and reason, not the text the model got.
    if (event.hook_blocked?.hook_id) return `Not run: hook ${event.hook_blocked.hook_id} blocked it${event.hook_blocked.reason ? `: ${clip(event.hook_blocked.reason)}` : ''}`
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
  if (name === 'remember') return 'Allow saving memory in this chat'
  return null
}

// The time an approval card expires, with the weekday when that is not today (limits reach 24 hours).
export function expiryTime(seconds, now = new Date()) {
  const when = new Date(seconds * 1000)
  const options = { hour: '2-digit', minute: '2-digit' }
  return when.toDateString() === now.toDateString() ? when.toLocaleTimeString([], options)
    : when.toLocaleString([], { weekday: 'short', ...options })
}

// The finished delegate_task result ({ status, output, changed_files, ... }) the parent agent received, or null.
function delegationResult(event) {
  if (event.name !== 'delegate_task' || event.state !== 'completed' || typeof event.output !== 'string') return null
  try {
    const result = JSON.parse(event.output)
    return result && typeof result === 'object' && typeof result.output === 'string' ? result : null
  } catch { return null }
}

const DELEGATION_STATUS = { awaiting_approval: 'Waiting for approval', queued: 'waiting for a free slot' }

function DelegationStatus({ event, onSelectSession, onOpenPath }) {
  const progress = event.delegation
  const running = !!progress && !progress.terminal_reason && typeof progress.started === 'number'
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (!running) return undefined
    setNow(Date.now() / 1000)
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(timer)
  }, [running])
  if (!progress) return null
  const status = DELEGATION_STATUS[progress.status] || String(progress.status || '').replace(/_/g, ' ')
  const kind = progress.agent_type || event.input?.agent_type
  const settings = agentSettingsText(progress.effort, progress.model)
  const started = typeof progress.started === 'number' ? progress.started : null
  // Ended without an end time (a restart interrupted it): no duration, rather than one that grows with every reload.
  const elapsed = started == null ? null : typeof progress.ended === 'number' ? progress.ended - started
    : running ? Math.max(now, started) - started : null
  const result = delegationResult(event)
  const files = Array.isArray(result?.changed_files) ? result.changed_files : []
  return html`<div class=${`delegation-progress ${progress.terminal_reason ? `delegation-${progress.status}` : 'delegation-live'}`} aria-label="Subagent progress">
    <span class="delegation-status">Subagent: ${status}</span>
    ${kind ? html`<span class="delegation-type">${agentLabel(kind)}</span>` : null}
    ${settings ? html`<span class="delegation-agent" title=${progress.model ? `Effort and model (${progress.model})` : 'Effort'}>${settings}</span>` : null}
    <span>${progress.completed_tools} completed ${progress.completed_tools === 1 ? 'action' : 'actions'}</span>
    ${progress.last_tool ? html`<span>Last tool: ${progress.last_tool.replace(/_/g, ' ')}</span>` : null}
    ${elapsed != null ? html`<span class="delegation-elapsed" title=${progress.terminal_reason ? 'Time it ran' : 'Time running'}>${elapsedText(elapsed)}</span>` : null}
    ${progress.terminal_reason ? html`<span>Reason: ${progress.terminal_reason.replace(/_/g, ' ')}</span>` : null}
    ${event.child_session_id && onSelectSession ? html`<button class="outline" onClick=${() => onSelectSession(event.child_session_id)}>Open subagent</button>` : null}
    ${result ? html`<details class="delegation-report"><summary>Report</summary>
      <${Markdown} text=${result.output} onOpenPath=${onOpenPath} />
      ${files.length ? html`<p class="delegation-files"><strong>Files changed:</strong> ${files.join(', ')}${result.changed_files_total > files.length ? ` and ${result.changed_files_total - files.length} more` : ''}</p>` : null}
      <small class="muted">${[Number.isInteger(result.steps_used) ? `${result.steps_used} ${result.steps_used === 1 ? 'step' : 'steps'}` : '',
        Number.isInteger(result.tool_calls) ? `${result.tool_calls} tool ${result.tool_calls === 1 ? 'call' : 'calls'}` : '',
        result.transcript ? `Transcript: ${result.transcript}` : ''].filter(Boolean).join(' · ')}</small>
    </details>` : null}
  </div>`
}

// Tools whose input names one file: the file the card's Open file button opens.
const FILE_INPUTS = { read_file: 'path', write_file: 'path', edit_file: 'path', insert_activity_log: 'path',
  notebook_read: 'path', notebook_edit: 'path', move_path: 'destination', outline_file: 'path' }
// Tools whose heading names input.path (summarizeTools shortens it).
const PATH_SUBJECTS = new Set([...Object.keys(FILE_INPUTS), 'delete_path', 'list_files', 'find_files', 'search_files', 'project_overview'])

// What the agent received when it differs from the card (the per-response context allowance).
function allowanceNote(event) {
  const withheld = event.result_withheld
  if (withheld) {
    const estimated = Number(withheld.estimated_tokens), allowance = Number(withheld.allowance_tokens)
    // As result_budget.deferred_text tells the model: one result larger than the whole allowance.
    if (estimated > allowance) return `Result not sent to the agent: it is larger than the step's context allowance (about ${estimated.toLocaleString('en-US')} of ${allowance.toLocaleString('en-US')} tokens). The agent was asked to read a smaller part.`
    return 'Result not sent to the agent: earlier results in the same step filled its context allowance. The agent can run it again.'
  }
  if (event.result_shortened) return 'Result shortened for the agent to fit its context allowance.'
  return null
}

// Shorter heading labels for narrow chat columns (styles.css swaps them in; the heading's name keeps the full label).
const SHORT_STATUS = { 'started in background': 'background' }

// "45 s", "10 min", "1 h": a command's time limit (run_command takes 1 to 3,600 seconds).
const limitText = seconds => seconds >= 3600 && seconds % 3600 === 0 ? `${seconds / 3600} h`
  : seconds >= 60 && seconds < 3600 && seconds % 60 === 0 ? `${seconds / 60} min` : elapsedText(seconds)

// "8:42", "1:02:03": the short form of a running time for narrow chat columns.
const clockText = seconds => {
  const total = Math.max(0, Math.round(seconds))
  const [hours, minutes, rest] = [Math.floor(total / 3600), Math.floor(total / 60) % 60, String(total % 60).padStart(2, '0')]
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`
}

// How long a running command has run against its time limit, counted each second: "running · 8 min 42 s of 40 min"
// (a narrow chat column shows "8:42"). started: epoch seconds (the event's job_started).
function CommandClock({ started, limit }) {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(timer)
  }, [])
  const seconds = Math.max(0, now - started)
  return html`<span class="tool-status neutral tool-clock" data-short=${clockText(seconds)}>running · ${elapsedText(seconds)} of ${limitText(limit)}</span>`
}

// The newest non-empty line of a running command's output (a progress line ended by a carriage return counts).
const latestLine = output => clip(String(output || '').slice(-4000).split(/\r\n|\r|\n/).map(line => line.trim()).filter(Boolean).at(-1) || '')

function ToolCard({ event, summary, result, status, failure = null, sessionId, onError, onSelectSession, onOpenPath }) {
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
  const hooked = hookSummary(event)
  const hints = hintText(event.mcp_annotations)
  // A running agent command shows its time against its limit and, collapsed, its latest output line.
  const runningCommand = event.state === 'running' && event.name === 'run_command'
  const clock = runningCommand && typeof event.job_started === 'number'
  const limit = Number.isInteger(event.input?.timeout_seconds) && event.input.timeout_seconds > 0 ? event.input.timeout_seconds : 60
  const liveLine = runningCommand && !expanded ? latestLine(event.output) : ''
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
      aria-label=${[title, summary.subject, summary.taskStatus ? `Task: ${summary.taskStatus}` : '', status.label].filter(Boolean).join(' ')}>
      ${event.name?.toLowerCase().includes('command') || event.name === 'Bash' ? html`<${Terminal} size=${16} />` : html`<${FileText} size=${16} />`}
      <span class="tool-name">${title}</span><span class="tool-subject" title=${summary.subject}>${summary.shown ?? summary.subject}</span>
      ${summary.taskStatus ? html`<span class="tool-task-status">Task: ${summary.taskStatus}</span>` : null}
      ${status.tone === 'running' ? html`<${LoaderCircle} size=${14} className="spin" />` : status.tone === 'ok' ? html`<${Check} size=${14} className="success-icon" />`
        : status.tone === 'warn' ? html`<${CircleAlert} size=${14} className="warning-icon" />` : null}
      ${status.label ? html`<span class=${`tool-status ${status.labelTone === 'neutral' ? 'neutral' : ''}`} data-short=${SHORT_STATUS[status.label]}>${status.label}</span>` : null}
      ${clock ? html`<${CommandClock} started=${event.job_started} limit=${limit} />` : null}
      ${expanded || pending ? html`<${ChevronDown} size=${14} />` : html`<${ChevronRight} size=${14} />`}
    </button>
    <${DelegationStatus} event=${event} onSelectSession=${onSelectSession} onOpenPath=${onOpenPath} />
    ${liveLine ? html`<p class="tool-live" title=${liveLine}>${liveLine}</p>` : null}
    ${rule ? html`<p class="tool-rule">${rule}</p>` : null}
    ${hooked ? html`<p class="hook-outcome">${hooked}</p>` : null}
    ${hints ? html`<p class="tool-hints" title="Hints from the MCP server. They never change approvals.">Server says: ${hints}</p>` : null}
    ${expanded || pending ? html`<div class="tool-body">
      ${pending ? html`<p>Local needs your approval to ${title.toLowerCase()}.${event.approval_expires
          ? ` If you do not answer by ${expiryTime(event.approval_expires)}, it will not run.` : ''}</p>
        <${ApprovalOptions} event=${event} sessionId=${sessionId} onError=${onError} deciding=${deciding} onDecide=${decide} allowInChat=${allowInChat}
          details=${html`<${ToolDetails} event=${event} pending=${true} />`}
          declineNote=${html`<label class="decline-note"><span>Tell the agent why (optional)</span>
            <textarea rows=${2} maxLength=${2000} spellcheck=${false} value=${note} disabled=${deciding}
              onInput=${e => setNote(e.currentTarget.value)} /></label>`} />`
        : html`${event.state === 'completed' && allowanceNote(event) ? html`<p class="tool-allowance">${allowanceNote(event)}</p>` : null}
          <${ToolDetails} event=${event} pending=${false} result=${result} failure=${failure} />
          ${Array.isArray(event.mcp_images) && event.mcp_images.length ? html`<${McpImages} images=${event.mcp_images} sessionId=${sessionId} />` : null}
          <small class="muted">Action: ${actionText(event, status)}${status.label ? html` · <span class="tool-status-text">${status.label}</span>` : ''}</small>
          ${onOpenPath && typeof filePath === 'string' && filePath ? html`<button class="outline tool-open-file"
            onClick=${() => onOpenPath(startLine ? { path: filePath, line: startLine } : { path: filePath })}><${FileText} size=${14} />Open file</button>` : null}`}
    </div>` : null}
    ${!expanded && (event.state === 'rejected' || event.state === 'cancelled' || event.state === 'error' || status.tone === 'warn')
      ? html`<p class="tool-outcome">${(result && failureOutcome(event, result, status)) || toolOutcome(event)}</p>` : null}
    ${!expanded && event.state === 'completed' && allowanceNote(event) ? html`<p class="tool-outcome">${allowanceNote(event)}</p>` : null}
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

// What user_prompt hooks did to a message (lifecycle_hooks.prompt_hooks): hook_blocked ({ hook_id, reason }: never
// sent), hook_edit ({ hooks, original }: the text shown is the hook's replacement) and hook_notes ([{ event, hook_id,
// context }]: context the model received after the message).
function HookMessageLabel({ event }) {
  if (event.hook_blocked) return html`<small class="hook-label">Not sent: blocked by hook ${event.hook_blocked.hook_id}${event.hook_blocked.reason ? ` (${clip(event.hook_blocked.reason, 160)})` : ''}</small>`
  if (event.hook_edit) return html`<small class="hook-label">Edited by hook ${(event.hook_edit.hooks || []).join(', ')}</small>`
  return null
}

function HookMessageDetails({ event }) {
  const notes = Array.isArray(event.hook_notes) ? event.hook_notes : []
  return html`${event.hook_edit ? html`<details class="hook-original"><summary>Your original message</summary><p>${event.hook_edit.original}</p></details>` : null}
    ${notes.length ? html`<details class="hook-notes"><summary>Hook context sent with this message</summary>
      <ul>${notes.map((note, index) => html`<li key=${index}><strong>${String(note.event || '').replace(/_/g, ' ')} hook ${note.hook_id}:</strong> ${note.context}</li>`)}</ul></details>` : null}`
}

function ActionButton({ label, icon: Icon, onClick, disabled = false, target }) {
  return html`<button class="icon-button message-action" aria-label=${label} title=${label} disabled=${disabled} data-target=${target} onClick=${onClick}><${Icon} size=${14} /></button>`
}

const wholeSeconds = value => Math.max(1, Math.round(value))

// The reply's thinking, like Claude's: open with live text while the model thinks ("Thinking… 12 s"), collapsed to
// "Thought for 12 s" once answer text or a tool call starts or the response ends, unless the user opened or closed
// it. started: epoch seconds (AgentEvent.reasoning_started); seconds: AgentEvent.reasoning_seconds. Without
// text, an endpoint that thought (a duration or reported reasoning tokens) gets one quiet line.
function Thinking({ text, truncated, started, seconds, streaming, answering, reasoningTokens }) {
  const thinking = streaming && !answering && typeof seconds !== 'number'
  const [manual, setManual] = useState(null)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!thinking || typeof started !== 'number') return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [thinking, started])
  // Timed from its start only while the response streams; a finished reply without a duration (cut short by a
  // restart) shows none, rather than one that grows with every reload.
  const elapsed = typeof seconds === 'number' ? seconds : streaming && typeof started === 'number' ? Math.max(0, now / 1000 - started) : null
  const label = thinking ? `Thinking…${elapsed >= 1 ? ` ${Math.floor(elapsed)} s` : ''}`
    : elapsed !== null ? `Thought for ${wholeSeconds(elapsed)} s` : 'Thought'
  if (!text) {
    if (typeof started !== 'number' && typeof seconds !== 'number' && !(reasoningTokens > 0)) return null
    if (thinking) return html`<p class="thinking-line">${label}</p>`
    return html`<p class="thinking-line">${label} · the endpoint did not return a summary</p>`
  }
  const open = manual ?? thinking
  return html`<details class="thinking" open=${open}>
    <summary onClick=${event => { event.preventDefault(); setManual(!open) }}>${label}</summary>
    <div class="thinking-body"><${Markdown} text=${text} streaming=${streaming} />
      ${truncated ? html`<p class="thinking-note">Thinking display limit reached.</p>` : null}</div>
  </details>`
}

function Reply({ text, reasoning, truncated, started, seconds, wait, requestInfo, onOpenPath, actions = null }) {
  const streaming = requestInfo?.status === 'running'
  return html`<div class="assistant-reply"><${Brand} small=${true} />
    <div class="reply-content"><${Thinking} text=${reasoning} truncated=${truncated} started=${started} seconds=${seconds}
      streaming=${streaming} answering=${!!text} reasoningTokens=${requestInfo?.usage?.reasoning_tokens} /><${Markdown} text=${text} streaming=${streaming} onOpenPath=${onOpenPath} />
      ${text || actions ? html`<div class="message-actions">${text ? html`<${CopyAction} text=${text} label="Copy response" />` : null}${actions}</div>` : null}
      ${requestInfo ? html`<${RequestDetails} info=${requestInfo} reasoningWait=${wait} reasoningText=${!!reasoning} />` : null}
    </div></div>`
}

// The cards waiting for your answer (an approval, a question, a plan): one per pending tool event, in transcript order.
export const PENDING_CARDS = '.tool-card.needs-approval, .question-card.needs-answer, .plan-card.needs-answer'

// Scrolls a waiting card into view and, with focus, moves keyboard focus to where answering starts: a tool card's
// heading (it names the action; Tab reaches the buttons), a question's first option, a plan's first review button.
// A card taller than the transcript cannot be shown whole; the view then keeps in sight the focused control (a long
// plan's review buttons on a phone, where they are not sticky), or without focus an approval card's end with its
// buttons.
export function revealCard(card, focus = false) {
  const box = card?.closest('.transcript')
  if (!box) return
  const tool = card.matches('.tool-card')
  const target = !focus ? null : tool ? card.querySelector('.tool-heading')
    : card.querySelector('input:not(:disabled)') || card.querySelector(':is(.plan-actions, .approval-buttons) button:not(:disabled)')
  // Where the view ends up is found with instant scrolls (the browser's own 'nearest', scroll-padding included) and
  // then reached with the transcript's smooth scroll from where it was.
  const from = box.scrollTop
  card.scrollIntoView({ block: 'nearest', behavior: 'instant' })
  const top = box.getBoundingClientRect().top + box.clientTop
  const seen = element => (place => place.top >= top - 0.5 && place.bottom <= top + box.clientHeight + 0.5)(element.getBoundingClientRect())
  if (target && !seen(target)) target.scrollIntoView({ block: 'nearest', behavior: 'instant' })
  else if (!target && tool && !seen(card)) card.scrollIntoView({ block: 'end', behavior: 'instant' })
  const to = box.scrollTop
  box.scrollTo({ top: from, behavior: 'instant' })
  box.scrollTo({ top: to })
  target?.focus({ preventScroll: true })
}

// onOpenPath({ path, line? }) opens a file named in a reply or a tool card in the Workspace panel.
// onLoadEarlier() -> Promise loads the page of messages before the first loaded one (session.events_start > 0).
// busy: the chat is not idle (message actions other than Copy are hidden).
// onPrefill(text) puts text in an empty composer and returns false when a typed draft was kept.
// roots: absolute folders besides the chat's allowed folders (the default code folder) whose files tool cards name
// as "<folder name>/rest".
export default function Conversation({ session, onError, onSelectSession, onOpenPath, busy = false, onPrefill, onLoadEarlier, roots = [] }) {
  const container = useRef(null)
  const bottom = useRef(null)
  const follow = useRef(true)
  // Prepending earlier messages keeps the visible ones in place: the scroll position moves by the
  // added height at once (the transcript otherwise scrolls smoothly). When the last page arrives
  // the focused button is gone, so focus moves to the transcript.
  const anchor = useRef(null)
  useLayoutEffect(() => {
    const el = container.current
    if (el && anchor.current) {
      el.scrollTo({ top: anchor.current.top + el.scrollHeight - anchor.current.height, behavior: 'instant' })
      if (anchor.current.focused && !el.contains(document.activeElement)) el.focus({ preventScroll: true })
    }
    anchor.current = null
  }, [session.events_start])
  const loadEarlier = event => {
    const el = container.current
    anchor.current = el ? { top: el.scrollTop, height: el.scrollHeight, focused: document.activeElement === event?.currentTarget } : null
    void Promise.resolve(onLoadEarlier?.()).catch(error => { anchor.current = null; onError?.(error.message) })
  }
  const rootsKey = JSON.stringify([...(session.allowed_directories || []), ...roots])
  const shorten = useMemo(() => pathShortener(session.workspace, JSON.parse(rootsKey)), [session.workspace, rootsKey])
  const projectName = String(session.workspace || '').split('/').filter(Boolean).at(-1) || ''
  const toolSummaries = useMemo(() => summarizeTools(session.events, shorten, projectName), [session.events, shorten, projectName])
  // Map<event id, { result, status }>: parsed once per event (see toolState).
  const toolStates = useMemo(() => new Map(session.events.filter(event => event.type === 'tool').map(event => [event.id, toolState(event)])), [session.events])
  const last = session.events[session.events.length - 1]
  useEffect(() => { if (follow.current) bottom.current?.scrollIntoView({ block: 'end' }) }, [last?.id, last?.text, last?.state])
  useEffect(() => { follow.current = true; bottom.current?.scrollIntoView({ block: 'end' }) }, [session.id])
  // A card that starts waiting for you is scrolled into view, also when you scrolled up to read: it is the one moment
  // you must act. Only when it starts waiting, so reading older output is not disturbed afterwards; while the view
  // follows the end, the scroll above already shows the last card.
  const waitingIds = useRef(null)
  // The card that just started waiting, until the 'awaiting_approval' status that usually follows it.
  const justWaiting = useRef(null)
  const pendingIds = () => session.events.filter(event => event.type === 'tool' && event.state === 'pending').map(event => event.id)
  useEffect(() => {
    const waiting = pendingIds()
    const before = waitingIds.current
    waitingIds.current = new Set(waiting)
    if (!before) return
    const index = waiting.findLastIndex(id => !before.has(id))
    if (index < 0) return
    justWaiting.current = session.status === 'awaiting_approval' ? null : waiting[index]
    if (follow.current && waiting[index] === last?.id) return
    revealCard(container.current?.querySelectorAll(PENDING_CARDS)[index])
  }, [session.events])
  // That status puts a hint under the message box, which makes the transcript shorter and can cut off the bottom of
  // the card, where Approve is: the card is brought into view again, once.
  useEffect(() => {
    const id = justWaiting.current
    if (session.status !== 'awaiting_approval' || id == null) return
    justWaiting.current = null
    const index = pendingIds().indexOf(id)
    if (index >= 0) revealCard(container.current?.querySelectorAll(PENDING_CARDS)[index])
  }, [session.status])
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
  // The rewound message is gone from the transcript (and its control with it): the message box is where to go on.
  const focusMessageBox = () => document.querySelector('textarea[aria-label="Message"]')?.focus()
  // After a conversation rewind whose resend failed, the text to send waits in the message box.
  const resent = (result, text) => {
    const draftKept = result.resend_error && onPrefill ? onPrefill(text) === false : false
    rewound({ backupId: result.backup_session_id }, draftKept)
    if (result.resend_error) onError(result.resend_error)
    focusMessageBox()
  }
  const retry = async () => {
    if (retrying || !cuts.latestUser) return
    setRetrying(true)
    try {
      const result = await api(`/sessions/${encodeURIComponent(session.id)}/rewind`, 'POST',
        { before_event_id: cuts.latestUser.id, conversation: true, resend_text: cuts.latestUser.text })
      resent(result, cuts.latestUser.text)
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
  return html`<div class="transcript" ref=${container} tabIndex=${-1} onScroll=${() => {
    const el = container.current
    if (el) follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  }}><div class="transcript-inner">
    ${session.events_start > 0 && onLoadEarlier ? html`<button class="outline load-earlier" onClick=${loadEarlier}>Load earlier messages (${session.events_start} more)</button>` : null}
    ${tools.length ? html`<details class="activity-summary"><summary>Activity · ${tools.length} ${tools.length === 1 ? 'action' : 'actions'}</summary>
      <ol>${tools.map(event => {
        const summary = toolSummaries.get(event.id)
        return html`<li key=${event.id}><span>${(event.name || 'Tool').replace(/_/g, ' ')}</span>${summary.subject ? html`<span class="activity-subject"> · ${summary.subject}</span>` : null}
          ${summary.taskStatus ? html`<small>Task: ${summary.taskStatus}</small>` : null}<small>Action: ${actionText(event, toolStates.get(event.id)?.status)}${(label => label ? ` · ${label}` : '')(toolStates.get(event.id)?.status.label)}</small></li>`
      })}</ol>
    </details>` : null}
    ${session.parent_session_id && onSelectSession ? html`<button class="outline parent-conversation" onClick=${() => onSelectSession(session.parent_session_id)}>Back to parent conversation</button>` : null}
    ${session.events.map(event => {
      if (event.type === 'user' && editing === event.id && idle) return html`<${EditMessage} key=${event.id} sessionId=${session.id} event=${event}
        onSelectSession=${onSelectSession}
        onCancel=${() => { editReturn.current = event.id; setEditing(null) }} onSent=${(result, text) => {
          setEditing(null)
          resent(result, text)
        }} />`
      if (event.type === 'user') return html`<div class=${`user-message${event.steered ? ' steered' : ''}${event.hook_blocked ? ' hook-blocked' : ''}`} key=${event.id}>${event.steered ? html`<small class="steered-label">Sent while the agent was working</small>` : null}${event.origin?.kind === 'delegated' ? html`<small class="delegation-origin">Delegated by parent conversation</small>` : null}<${HookMessageLabel} event=${event} />${event.text}<${Attachments} items=${event.attachments} /><${HookMessageDetails} event=${event} />${event.child_session_id && onSelectSession ? html`<button class="outline" onClick=${() => onSelectSession(event.child_session_id)}>Open subagent</button>` : null}${event.text || idle ? html`<div class="message-actions">${event.text ? html`<${CopyAction} text=${event.text} label="Copy message" />` : null}${idle ? html`<${ActionButton} label="Edit message" icon=${Pencil} target=${`edit:${event.id}`} onClick=${() => setEditing(event.id)} /><${ActionButton} label="Rewind to here" icon=${Undo2} onClick=${() => setRewinding(event)} />` : null}</div>` : null}</div>`
      if (event.type === 'tool' && event.name === 'ask_user') return html`<${QuestionCard} key=${event.id} event=${event} sessionId=${session.id} onError=${onError} />`
      if (event.type === 'tool' && event.name === 'submit_plan') return html`<${PlanCard} key=${event.id} event=${event} sessionId=${session.id} onError=${onError} />`
      if (event.type === 'tool') return html`<${ToolCard} key=${event.id} event=${event} summary=${toolSummaries.get(event.id)} result=${toolStates.get(event.id).result} status=${toolStates.get(event.id).status} failure=${toolStates.get(event.id).failure} sessionId=${session.id} onError=${onError} onSelectSession=${onSelectSession} onOpenPath=${onOpenPath} />`
      if (event.type === 'assistant') return event.text || event.reasoning_summary || event.request_info || event.reasoning_started ? html`<${Reply} key=${event.id} text=${event.text || ''} reasoning=${event.reasoning_summary} truncated=${event.reasoning_truncated} started=${event.reasoning_started} seconds=${event.reasoning_seconds} wait=${event.reasoning_wait_seconds} requestInfo=${event.request_info} onOpenPath=${onOpenPath} actions=${event.text ? replyActions(event) : null} />` : null
      const marker = event.type !== 'error' && (!!event.stop_phase || event.text === STOPPED_TEXT)
      const classes = ['conversation-notice', event.type === 'error' && 'error', marker && 'stop-marker', event.compaction && 'compaction'].filter(Boolean).join(' ')
      if (marker) return html`<div class=${classes} key=${event.id} title=${event.text}>${STOP_TEXT[event.stop_phase] || 'Stopped'}</div>`
      const error = event.type === 'error' ? noticeError(event) : null
      // A rate-limit notice's Details: the 429's code, message and Retry-After (AgentEvent.details).
      const details = error ? error.details : typeof event.details === 'string' && event.details ? event.details : null
      return html`<div class=${classes} key=${event.id} role=${error ? 'note' : undefined}>${error ? html`<p class="notice-summary">${error.summary}</p>` : idle ? idleNotice(event.text) : event.text}${details
          ? html`<details class="notice-details"><summary>Details</summary><pre>${details}</pre></details>` : null}<${DelegationStatus} event=${event} onSelectSession=${onSelectSession} />${!event.delegation && event.child_session_id && onSelectSession ? html`<button class="outline" onClick=${() => onSelectSession(event.child_session_id)}>Open subagent</button>` : null}</div>`
    })}
    ${['running', 'delegating'].includes(session.status) ? html`<div class="working-indicator"><span /><span /><span /><span class="sr-only">Working</span></div>` : null}
    ${rewinding && idle ? html`<${RewindDialog} session=${session} event=${rewinding} onClose=${() => setRewinding(null)}
      onSelectSession=${onSelectSession}
      onRewound=${result => {
        setRewinding(null)
        rewound(result, result.conversation && onPrefill ? onPrefill(rewinding.text || '') === false : false)
        if (result.conversation) focusMessageBox()
      }} />` : null}
    ${forking !== undefined && onSelectSession ? html`<${ForkDialog} session=${session} beforeEventId=${forking.beforeEventId}
      onSelectSession=${onSelectSession} onClose=${() => setForking(undefined)} />` : null}
    <div ref=${bottom} />
  </div></div>`
}
