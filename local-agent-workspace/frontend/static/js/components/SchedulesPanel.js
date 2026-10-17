import { html, useCallback, useEffect, useRef, useState } from '../lib.js'
import { api } from '../api.js'
import { EFFORT_LABELS } from '../types.js'
import { modeLabel } from './PermissionsMenu.js'
import { durationText, tokenText } from '../turnResult.js'

// Agent tools → Schedules: this chat's project's scheduled runs (backend schedules.py): a prompt sent to a new chat
// at set times, with a fixed permission mode (never Bypass), an approval limit, a token and a time limit per run and a
// spending limit. Run policy Ask (the default) waits for Run or Skip; Automatic runs without a click.
// Props: session (null: the default project), onError, onSelectSession(id) (opens a run's chat)
const scope = session => session ? `?session_id=${encodeURIComponent(session.id)}` : ''
const count = (value, one, many = `${one}s`) => `${value.toLocaleString('en-US')} ${value === 1 ? one : many}`
const when = stamp => typeof stamp === 'number'
  ? new Date(stamp * 1000).toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'
// The modes a schedule can use (backend schedules.MODES): Don't ask first, as it never waits for a card.
const MODES = ['dontAsk', 'plan', 'manual', 'acceptEdits', 'auto']
const TRIGGERS = { scheduled: 'Scheduled', catch_up: 'Caught up', run_now: 'Run now', ready: 'Run when ready' }
const ENDS = { completed: 'Completed', tool_error: 'Completed with a failed tool', step_limit: 'Reached the step limit',
  spending_limit: 'Reached the spending limit', budget_limit: 'Reached its token or time limit',
  approval_expired: 'Stopped at the approval limit', stopped: 'Stopped', interrupted: 'Interrupted', error: 'Ended with an error',
  inference_error: 'Ended with an error', hook_blocked: 'Blocked by a hook', deleted: 'Its chat was deleted' }
const PAUSED = { paused: 'Paused', token_rejected: 'Paused: Databricks rejected the token', invalid: 'Paused: its saved schedule is not valid' }
const BLANK = { name: '', prompt: '', spec: '', timezone: '', model: '', reasoning_effort: '', permission_mode: 'dontAsk',
  approval_timeout_minutes: 10, turn_token_limit: 500000, turn_time_limit_minutes: 30, spending_limit_dbu: 0, max_agent_steps: null,
  run_policy: 'ask', catch_up: 'once' }
const FIELDS = Object.keys(BLANK)

// What a run in the history shows: its outcome and, once it ended, how; a note says why one did not run.
function outcomeText(entry) {
  if (entry.outcome === 'started') {
    if (!('ended' in entry)) return 'Running'
    return ENDS[entry.terminal_reason] || String(entry.terminal_reason || 'Ended').replace(/_/g, ' ')
  }
  return entry.note || (entry.outcome === 'skipped' ? 'Skipped' : 'Not run')
}

function runDetails(entry) {
  const parts = []
  if (Number.isInteger(entry.steps)) parts.push(count(entry.steps, 'step'))
  if (typeof entry.duration_s === 'number') parts.push(durationText(entry.duration_s))
  if (Number.isInteger(entry.tokens)) parts.push(`${tokenText(entry.tokens)} tokens`)
  if (typeof entry.estimated_dbu === 'number') parts.push(`~${entry.estimated_dbu >= 1 ? entry.estimated_dbu.toFixed(1) : entry.estimated_dbu.toFixed(3)} DBU`)
  return parts.join(' · ')
}

// The form's own checks, before the server's (which say the same): the caps are required.
function formProblem(form, limits) {
  const whole = (value, low, high) => Number.isInteger(value) && value >= low && value <= high
  if (!form.name.trim()) return 'Name the schedule.'
  if (!form.prompt.trim()) return 'Enter the prompt each run sends.'
  if (!form.spec.trim()) return 'Enter when it runs.'
  if (!whole(form.approval_timeout_minutes, 1, limits.max_approval_minutes || 60)) return `Set an approval limit of 1 to ${limits.max_approval_minutes || 60} minutes.`
  if (!whole(form.turn_token_limit, 1, limits.max_turn_tokens || 1e8)) return 'Set a token limit per run: scheduled runs need one.'
  if (!whole(form.turn_time_limit_minutes, 1, limits.max_turn_minutes || 1440)) return 'Set a time limit per run: scheduled runs need one.'
  if (!Number.isFinite(form.spending_limit_dbu) || form.spending_limit_dbu < 0) return 'Set a spending limit of 0 (the Settings default) or more DBU.'
  if (form.max_agent_steps !== null && !whole(form.max_agent_steps, 1, 64)) return 'Agent steps must be empty or 1 to 64.'
  return ''
}

export default function SchedulesPanel({ session, onError, onSelectSession }) {
  const query = scope(session)
  const [listing, setListing] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [editing, setEditing] = useState(null)  // { id (null: new), form } while the form is open
  const alive = useRef(false)
  const report = useRef(onError)
  report.current = onError
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const run = useCallback(async (operation, done) => {
    setBusy(true); setError(''); setNotice('')
    try {
      const value = await operation()
      if (alive.current) { setListing(value); done?.(value) }
    } catch (e) {
      if (alive.current) { setError(e.message); report.current(e.message) }
    } finally { if (alive.current) setBusy(false) }
  }, [])
  const refresh = useCallback(() => run(() => api(`/schedules${query}`)), [query, run])
  useEffect(() => { void refresh() }, [refresh])
  const items = listing?.schedules || []
  const limits = listing?.limits || {}
  const act = (item, action, text) => void run(() => api(`/schedules/${encodeURIComponent(item.id)}/${action}${query}`, 'POST'), () => setNotice(text))
  const remove = item => {
    if (!window.confirm(`Delete the schedule ${item.name}? Its runs' chats stay.`)) return
    void run(() => api(`/schedules/${encodeURIComponent(item.id)}${query}`, 'DELETE'), () => { if (editing?.id === item.id) setEditing(null); setNotice('Schedule deleted.') })
  }
  const runNow = item => void run(() => api(`/schedules/${encodeURIComponent(item.id)}/run${query}`, 'POST'), () => setNotice('Run started. Its chat is in the history below.'))
  return html`<section class="agent-tools-section schedules-panel" aria-label="Schedules">
    <div class="agent-tools-section-heading"><h3>Scheduled runs</h3>
      <div class="agent-tools-actions"><button class="outline" disabled=${busy} onClick=${() => void refresh()}>Refresh schedules</button>
        <button class="primary" disabled=${busy || !listing || !!editing || items.length >= (limits.max_schedules || 50)} onClick=${() => setEditing({ id: null, form: { ...BLANK } })}>New schedule</button></div></div>
    <p>A schedule sends its prompt to a new chat of this project at set times, with a fixed permission mode and limits. Each run is a separate chat tagged scheduled, and makes model calls billed to your workspace.</p>
    ${listing && !listing.enabled ? html`<p class="settings-note" role="status">Scheduled runs are off in Settings → Agent: schedules do not run at their times. Run now still works.</p>` : null}
    ${listing?.problem ? html`<p class="form-error" role="alert">${listing.problem}</p>` : null}
    ${listing?.token_warning ? html`<p class="settings-note schedules-token" role="status">${listing.token_warning}</p>` : null}
    ${listing?.project ? html`<p class="field-help">Project: <code>${listing.project}</code>${listing.other_projects ? ` · ${count(listing.other_projects, 'other project')} with schedules` : ''}</p>` : null}
    ${editing ? html`<${ScheduleForm} key=${editing.id || 'new'} editing=${editing} limits=${limits} busy=${busy}
      onCancel=${() => setEditing(null)} onSave=${form => void run(() => editing.id
        ? api(`/schedules/${encodeURIComponent(editing.id)}${query}`, 'PUT', form) : api(`/schedules${query}`, 'POST', form),
      () => { setEditing(null); setNotice(editing.id ? 'Schedule saved.' : 'Schedule added.') })} />` : null}
    ${items.length ? html`<ul class="schedules-list" aria-label="Schedules">${items.map(item => html`<li class="schedule-item" key=${item.id}>
      <${ScheduleRow} item=${item} busy=${busy} editing=${!!editing} onRun=${() => runNow(item)} onSkip=${() => act(item, 'skip', 'Run skipped.')}
        onPause=${() => act(item, 'pause', 'Schedule paused.')} onResume=${() => act(item, 'resume', 'Schedule resumed.')}
        onEdit=${() => setEditing({ id: item.id, form: Object.fromEntries(FIELDS.map(key => [key, item[key] ?? BLANK[key]])) })}
        onDelete=${() => remove(item)} onOpen=${onSelectSession} />
    </li>`)}</ul>` : listing && !editing ? html`<p class="muted">No schedules in this project yet. Add one with New schedule.</p>` : null}
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}${notice ? html`<p role="status">${notice}</p>` : null}
    <p class="field-help">The app runs schedules only while it is open and Settings → Agent → Scheduled runs is on. Runs missed while it was closed are caught up once when it starts again (if the latest was due within 24 hours), never once per missed time. At most ${limits.max_running || 2} scheduled runs run at a time, and a run first checks the Databricks connection (no model call): a rejected token pauses the schedule.</p>
  </section>`
}

function limitsText(item) {
  return [modeLabel(item.permission_mode), item.run_policy === 'auto' ? 'Runs automatically' : 'Asks before each run',
    `cards wait ${count(item.approval_timeout_minutes, 'minute')}`,
    `${tokenText(item.turn_token_limit)} tokens and ${count(item.turn_time_limit_minutes, 'minute')} per run`,
    ...(item.spending_limit_dbu ? [`${item.spending_limit_dbu} DBU`] : [])].join(' · ')
}

function ScheduleRow({ item, busy, editing, onRun, onSkip, onPause, onResume, onEdit, onDelete, onOpen }) {
  const history = [...(item.history || [])].reverse()
  const state = item.running ? 'Running' : item.pending ? 'Waiting for Run' : !item.enabled ? PAUSED[item.paused_reason] || 'Paused' : null
  return html`<div class="schedule-head"><div class="schedule-text"><strong>${item.name}</strong>${state ? html` <span class="schedule-state">${state}</span>` : null}
      <small>${item.spec} · ${item.zone}${item.enabled && item.next_run ? ` · next ${when(item.next_run)}` : ''}</small>
      <small>${limitsText(item)}</small></div>
    <div class="agent-tools-actions">
      <button class="outline" disabled=${busy || item.running} aria-label=${`Run ${item.name} now`} onClick=${onRun}>Run now</button>
      ${item.enabled ? html`<button class="outline" disabled=${busy} aria-label=${`Pause ${item.name}`} onClick=${onPause}>Pause</button>`
        : html`<button class="outline" disabled=${busy} aria-label=${`Resume ${item.name}`} onClick=${onResume}>Resume</button>`}
      <button class="outline" disabled=${busy || editing} aria-label=${`Edit ${item.name}`} onClick=${onEdit}>Edit</button>
      <button class="outline" disabled=${busy} aria-label=${`Delete ${item.name}`} onClick=${onDelete}>Delete</button>
    </div></div>
    ${item.pending ? html`<div class="schedule-ready" role="group" aria-label=${`Run of ${item.name} waiting`}>
      <span>Ready since ${when(item.pending.created)}${item.pending.note ? ` · ${item.pending.note}` : ''}</span>
      <button class="primary" disabled=${busy || item.running} onClick=${onRun}>Run</button><button class="outline" disabled=${busy} onClick=${onSkip}>Skip</button></div>` : null}
    ${item.paused_reason === 'token_rejected' ? html`<p class="form-error">Databricks rejected the token at the last run. Renew it in your credential file, then Resume.</p>` : null}
    ${item.paused_note ? html`<p class="form-error">${item.paused_note}</p>` : null}
    <details class="schedule-history"><summary>History (${history.length})</summary>
      ${history.length ? html`<ol aria-label=${`Runs of ${item.name}`}>${history.map(entry => html`<li key=${entry.id}>
        <span><span class="schedule-when">${when(entry.at)}</span> · ${TRIGGERS[entry.trigger] || entry.trigger} · ${outcomeText(entry)}${runDetails(entry) ? ` · ${runDetails(entry)}` : ''}
          ${entry.outcome === 'started' && entry.note ? html`<small>${entry.note}</small>` : null}</span>
        ${entry.session_id && entry.terminal_reason !== 'deleted' ? html`<button class="outline" aria-label=${`Open the chat of the run at ${when(entry.at)}`} onClick=${() => onOpen(entry.session_id)}>Open chat</button>` : null}
      </li>`)}</ol>` : html`<p class="muted">No runs yet.</p>`}
    </details>`
}

function ScheduleForm({ editing, limits, busy, onCancel, onSave }) {
  const [form, setForm] = useState(editing.form)
  const [problem, setProblem] = useState('')
  const [preview, setPreview] = useState(null)  // { next_runs, zone } or { error } for the typed schedule
  const field = (key, value) => setForm(current => ({ ...current, [key]: value }))
  const whole = key => event => { const text = event.currentTarget.value.trim(); field(key, text === '' ? (key === 'max_agent_steps' ? null : NaN) : Number(text)) }
  const name = useRef(null)
  useEffect(() => { name.current?.focus() }, [])
  // The next three times of the typed schedule, checked by the server as you type (for an edit, from the saved
  // schedule's start while its timing stays the same).
  useEffect(() => {
    const spec = form.spec.trim()
    if (!spec) { setPreview(null); return undefined }
    let alive = true
    const timer = setTimeout(() => {
      api('/schedules/preview', 'POST', { spec, timezone: form.timezone.trim(), ...(editing.id ? { schedule_id: editing.id } : {}) })
        .then(value => { if (alive) setPreview(value) }, e => { if (alive) setPreview({ error: e.message }) })
    }, 300)
    return () => { alive = false; clearTimeout(timer) }
  }, [form.spec, form.timezone, editing.id])
  const submit = event => {
    event.preventDefault()
    const found = formProblem(form, limits)
    setProblem(found)
    if (found || busy) return
    onSave({ ...form, name: form.name.trim(), spec: form.spec.trim(), timezone: form.timezone.trim(), model: form.model.trim() })
  }
  const max = limits.max_approval_minutes || 60
  return html`<form class="agent-tools-form schedule-form" aria-label=${editing.id ? 'Edit schedule' : 'New schedule'} onSubmit=${submit} noValidate>
    <label>Name<input ref=${name} maxLength=${limits.max_name || 80} value=${form.name} onInput=${e => field('name', e.currentTarget.value)} placeholder="Nightly test run" /></label>
    <label>Prompt<textarea rows=${3} maxLength=${limits.max_prompt || 20000} value=${form.prompt} onInput=${e => field('prompt', e.currentTarget.value)}
      placeholder="Run the tests and summarize any failures." /></label>
    <label>When it runs<input spellcheck=${false} value=${form.spec} onInput=${e => field('spec', e.currentTarget.value)} placeholder="every 2 hours, or 0 9 * * 1-5" /></label>
    <p class="field-help">every N minutes, hours or days (from when you save that timing), or a cron line: minute hour day month weekday, such as 0 9 * * 1-5 for 9:00 on weekdays. At most every ${limits.min_interval_minutes || 5} minutes.</p>
    ${preview?.error ? html`<p class="form-error">${preview.error}</p>` : preview?.next_runs ? html`<p class="field-help schedule-preview" role="status">Next runs (${preview.zone}): ${preview.next_runs.map(when).join(' · ')}</p>` : null}
    <label>Time zone<input spellcheck=${false} value=${form.timezone} onInput=${e => field('timezone', e.currentTarget.value)} placeholder="This computer's" /></label>
    <label>Permission mode<select value=${form.permission_mode} onChange=${e => field('permission_mode', e.currentTarget.value)}>
      ${MODES.map(mode => html`<option key=${mode} value=${mode}>${modeLabel(mode)}${mode === 'dontAsk' ? ' (recommended)' : ''}</option>`)}</select></label>
    <p class="field-help">Fixed for every run; never Bypass permissions. Don't ask blocks what its rules do not allow instead of asking, so a run rarely waits for a card.</p>
    <label>Approval limit (minutes)<input type="number" min=${1} max=${max} step=${1} value=${Number.isFinite(form.approval_timeout_minutes) ? form.approval_timeout_minutes : ''} onInput=${whole('approval_timeout_minutes')} /></label>
    <p class="field-help">A card (an approval, a question or a plan) that waits this long without an answer ends the run; its action does not run. 1 to ${max}.</p>
    <div class="schedule-limits">
      <label>Token limit per run<input type="number" min=${1} step=${1} value=${Number.isFinite(form.turn_token_limit) ? form.turn_token_limit : ''} onInput=${whole('turn_token_limit')} /></label>
      <label>Time limit per run (minutes)<input type="number" min=${1} max=${limits.max_turn_minutes || 1440} step=${1} value=${Number.isFinite(form.turn_time_limit_minutes) ? form.turn_time_limit_minutes : ''} onInput=${whole('turn_time_limit_minutes')} /></label>
      <label>Spending limit per run (DBU)<input type="number" min=${0} step="any" inputmode="decimal" value=${Number.isFinite(form.spending_limit_dbu) ? form.spending_limit_dbu : ''}
        onInput=${e => { const text = e.currentTarget.value.trim(); field('spending_limit_dbu', text === '' ? NaN : Number(text)) }} /></label>
      <label>Agent steps<input type="number" min=${1} max=${64} step=${1} value=${form.max_agent_steps ?? ''} placeholder="Settings" onInput=${whole('max_agent_steps')} /></label>
    </div>
    <p class="field-help">Required: the token and time limits end a run as a turn's limits do (Settings → Agent). The spending limit counts estimated DBU (0 follows Settings → Agent → Spending limit per conversation); endpoints without price data are capped by the token limit only.</p>
    <div class="schedule-limits">
      <label>Model endpoint<input spellcheck=${false} value=${form.model} onInput=${e => field('model', e.currentTarget.value)} placeholder="The Settings model" /></label>
      <label>Reasoning effort<select value=${form.reasoning_effort} onChange=${e => field('reasoning_effort', e.currentTarget.value)}>
        <option value="">The Settings default</option>${Object.entries(EFFORT_LABELS).map(([value, label]) => html`<option key=${value} value=${value}>${label}</option>`)}</select></label>
    </div>
    <label>Run policy<select value=${form.run_policy} onChange=${e => field('run_policy', e.currentTarget.value)}>
      <option value="ask">Ask before each run (Run or Skip)</option><option value="auto">Run automatically</option></select></label>
    ${form.run_policy === 'auto' ? html`<p class="settings-note" role="status">Runs start at their times without a click, and each one makes model calls billed to your workspace.</p>`
      : html`<p class="field-help">At each time the app shows "Scheduled run ready" with Run and Skip (and a desktop notification when that is on); nothing is sent until you choose Run. A run nobody starts within 24 hours, or before the next time, is not run.</p>`}
    <label>Missed runs<select value=${form.catch_up} onChange=${e => field('catch_up', e.currentTarget.value)}>
      <option value="once">Run once when the app is back</option><option value="skip">Skip them</option></select></label>
    ${problem ? html`<p class="form-error" role="alert">${problem}</p>` : null}
    <div class="agent-tools-actions"><button type="button" class="outline" disabled=${busy} onClick=${onCancel}>Cancel</button>
      <button class="primary" disabled=${busy}>${editing.id ? 'Save schedule' : 'Add schedule'}</button></div>
  </form>`
}

// Above the chat, in every view: the runs waiting for Run (run policy Ask) and the schedules a rejected token paused,
// over all projects (GET /schedules/ready, every 30 s while Settings → Scheduled runs is on). Run starts the run and
// opens its chat. With browser notifications on (the sidebar's bell) a hidden page also shows a notification.
// Props: enabled, notify (browser notifications are on), onOpenChat(id), onOpenSchedules(), onError
export function ScheduleAlerts({ enabled, notify, onOpenChat, onOpenSchedules, onError }) {
  const [state, setState] = useState({ ready: [], paused: [] })
  const [busy, setBusy] = useState(false)
  const seen = useRef(new Set())
  const notifyRef = useRef(notify)
  notifyRef.current = notify
  const load = useCallback(async () => {
    let value
    try { value = await api('/schedules/ready') } catch { return }  // The session poll reports an unreachable server.
    setState(value)
    for (const item of value.ready || []) {
      if (seen.current.has(`${item.id}:${item.due}`)) continue
      seen.current.add(`${item.id}:${item.due}`)
      if (!notifyRef.current || !document.hidden || typeof window.Notification !== 'function' || window.Notification.permission !== 'granted') continue
      try { new window.Notification('Local', { body: `Scheduled run ready: ${item.name}`, tag: `schedule:${item.id}` }) }
      catch { /* Some browsers allow notifications only from a service worker. */ }
    }
  }, [])
  useEffect(() => {
    if (!enabled) { setState({ ready: [], paused: [] }); return undefined }
    void load()
    const timer = setInterval(() => void load(), 30000)
    return () => clearInterval(timer)
  }, [enabled, load])
  const act = async (item, action) => {
    setBusy(true)
    try {
      const result = await api(`/schedules/${encodeURIComponent(item.id)}/${action}`, 'POST')
      await load()
      if (action === 'run' && result.session_id) onOpenChat(result.session_id)
    } catch (e) { onError(e) } finally { setBusy(false) }
  }
  const ready = state.ready || []
  const paused = state.paused || []
  if (!ready.length && !paused.length) return null
  return html`<div class="schedule-alerts">
    ${ready.slice(0, 3).map(item => html`<div class="connection-banner schedule-alert" role="status" key=${item.id}>
      <span class="banner-text">Scheduled run ready: <strong>${item.name}</strong>${item.note ? ` · ${item.note}` : ''}</span>
      <div class="banner-actions"><button disabled=${busy} aria-label=${`Run ${item.name}`} onClick=${() => void act(item, 'run')}>Run</button>
        <button disabled=${busy} aria-label=${`Skip ${item.name}`} onClick=${() => void act(item, 'skip')}>Skip</button></div></div>`)}
    ${ready.length > 3 ? html`<div class="connection-banner schedule-alert" role="status"><span class="banner-text">${ready.length - 3} more scheduled runs are ready.</span>
      <div class="banner-actions"><button onClick=${onOpenSchedules}>Open schedules</button></div></div>` : null}
    ${paused.length ? html`<div class="connection-banner schedule-alert" role="alert"><span class="banner-text">Scheduled runs paused: Databricks rejected the token
      (${paused.map(item => item.name).join(', ')}). Renew it in your credential file, then resume them in Agent tools → Schedules.</span>
      <div class="banner-actions"><button onClick=${onOpenSchedules}>Open schedules</button></div></div>` : null}
  </div>`
}
