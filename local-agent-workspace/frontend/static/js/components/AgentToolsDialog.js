import { html, useCallback, useEffect, useRef, useState } from '../lib.js'
import { X } from '../icons.js'
import { api } from '../api.js'
import ConversationTransfer from './ConversationTransfer.js'
import ExtensionsEditor from './ExtensionsEditor.js'
import UsagePanel from './UsagePanel.js'
import { blockerOf, OpenBlockingChat } from './BlockingChat.js'
import { modeLabel } from './PermissionsMenu.js'
import { agentLabel, agentSettingsText, elapsedText } from '../subagents.js'
import { EFFORT_LABELS, modelLabel } from '../types.js'
import { hintText } from '../mcp.js'

// Props: session, workspace?, changedFiles? (files with unreviewed agent changes in this chat, from the
// changes bar; null when unknown), onClose, onSelectSession, onError, onSaveTools?(tools) -> Session
const profileLabels = { inherit: 'Inherit permitted tools', read_only: 'Read-only files', file_editor: 'Read and edit files' }
const taskStatuses = ['pending', 'in_progress', 'completed', 'cancelled']
const scopeQuery = session => session ? `?session_id=${encodeURIComponent(session.id)}` : ''
const plural = (count, one, many = `${one}s`) => `${count.toLocaleString('en-US')} ${count === 1 ? one : many}`

// The sections, grouped in the navigation on the left (one scrolling row below 640 px).
export const SECTIONS = [
  { group: 'This chat', tabs: ['Overview', 'Tasks', 'Subagents', 'Tools', 'Usage', 'Export'] },
  { group: 'Project', tabs: ['Checkpoints', 'Verify', 'Worktrees', 'Extensions'] },
]
const TABS = SECTIONS.flatMap(section => section.tabs)
const TAB_KEY = 'agent-tools-tab'

// The section shown last, remembered in this browser; Overview when none (or storage is unavailable).
function rememberedTab() {
  try {
    const value = localStorage.getItem(TAB_KEY)
    return TABS.includes(value) ? value : 'Overview'
  } catch { return 'Overview' }
}

function rememberTab(tab) {
  try { localStorage.setItem(TAB_KEY, tab) } catch { /* Blocked storage: the choice is not remembered. */ }
}

function useRequest(onError) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [blocker, setBlocker] = useState(null)  // The other chat that error names, if any.
  const alive = useRef(false)
  const report = useRef(onError)
  report.current = onError
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const run = useCallback(async (operation, success) => {
    setBusy(true); setError(''); setBlocker(null)
    try {
      const value = await operation()
      if (alive.current) success(value)
    } catch (e) {
      if (alive.current) { setError(e.message); setBlocker(blockerOf(e)); report.current(e.message) }
    } finally { if (alive.current) setBusy(false) }
  }, [])
  return { busy, error, blocker, run }
}

export default function AgentToolsDialog(props) {
  return html`<${AgentToolsScope} key=${props.session?.id || 'draft'} ...${props} />`
}

function AgentToolsScope(props) {
  const dialog = useRef(null)
  const nav = useRef(null)
  const [tab, setTab] = useState(rememberedTab)
  useEffect(() => { dialog.current?.showModal() }, [])
  // In the narrow layout the navigation is one scrolling row: keep the open section's button in view.
  useEffect(() => { nav.current?.querySelector('[aria-pressed="true"]')?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' }) }, [tab])
  const open = useCallback(name => { setTab(name); rememberTab(name) }, [])
  const sections = {
    // An Open button goes away with the Overview: keyboard focus moves to the opened section's button.
    Overview: () => html`<${Overview} ...${props} onOpen=${name => {
      open(name)
      setTimeout(() => nav.current?.querySelector('[aria-pressed="true"]')?.focus())
    }} />`,
    Tasks: () => html`<${Tasks} ...${props} />`,
    Subagents: () => html`<${Subagents} ...${props} />`,
    Tools: () => html`<${ToolsPanel} ...${props} />`,
    Usage: () => html`<${UsagePanel} session=${props.session} onError=${props.onError} />`,
    Export: () => html`<${ConversationTransfer} sessionId=${props.session?.id} workspace=${props.session?.workspace || props.workspace || ''}
      busy=${!!props.session && props.session.status !== 'idle'} onError=${props.onError} onSelectSession=${id => { props.onSelectSession(id); props.onClose() }} />`,
    Checkpoints: () => html`<${Checkpoints} ...${props} />`,
    Verify: () => html`<${Verify} ...${props} />`,
    Worktrees: () => html`<${Worktrees} ...${props} />`,
    Extensions: () => html`<${ExtensionsPanel} ...${props} />`,
  }
  return html`<dialog class="agent-tools-dialog" ref=${dialog} aria-label="Agent tools" onCancel=${props.onClose} onClick=${e => { if (e.target === dialog.current) props.onClose() }}>
    <div class="agent-tools-content">
      <header class="agent-tools-header"><div><h2>Agent tools</h2><p>${props.session?.title || 'Default workspace'}</p></div><button class="icon-button" aria-label="Close agent tools" onClick=${props.onClose}><${X} size=${18} /></button></header>
      <div class="agent-tools-layout">
        <nav class="agent-tools-tabs" ref=${nav} aria-label="Agent tools sections">${SECTIONS.map(section => html`<div class="agent-tools-group" role="group" aria-label=${section.group} key=${section.group}>
          <span class="agent-tools-group-label" aria-hidden="true">${section.group}</span>
          ${section.tabs.map(name => html`<button key=${name} aria-pressed=${tab === name} onClick=${() => open(name)}>${name}</button>`)}
        </div>`)}</nav>
        <div class="agent-tools-body">${sections[tab]()}</div>
      </div>
    </div>
  </dialog>`
}

// Overview: this chat and its project at a glance, from the routes the sections use (nothing new or
// heavy). A value that cannot be loaded says so here; its section shows the error.
function useOverview(session) {
  const [data, setData] = useState({})
  const id = session?.id
  useEffect(() => {
    let alive = true
    const query = id ? `?session_id=${encodeURIComponent(id)}` : ''
    const path = id ? `/sessions/${encodeURIComponent(id)}` : null
    const requests = {
      checkpoints: () => api(`/checkpoints${query}`), project: () => api(`/project-settings${query}`),
      extensions: () => api('/extensions'), worktrees: () => api(`/worktrees${query}`),
      ...(path ? { tools: () => api(`/tools${query}`), children: () => api(`${path}/children`), tasks: () => api(`${path}/tasks`) } : {}),
    }
    for (const [key, request] of Object.entries(requests)) {
      Promise.resolve().then(request).then(value => { if (alive) setData(current => ({ ...current, [key]: { value } })) },
        () => { if (alive) setData(current => ({ ...current, [key]: { failed: true } })) })
    }
    return () => { alive = false }
  }, [id])
  return data
}

function toolsText({ tools = [], disabled = [] }) {
  const names = tools.map(tool => tool.name)
  const off = names.filter(name => disabled.includes(name)).length
  const servers = disabled.filter(name => MCP_SERVER_SWITCH.test(name)).length
  const mcp = disabled.filter(name => !names.includes(name) && !MCP_SERVER_SWITCH.test(name)).length
  return `${names.length - off} of ${names.length} on` + (mcp ? ` · ${plural(mcp, 'MCP tool')} off` : '')
    + (servers ? ` · ${plural(servers, 'MCP server')} off` : '')
}

// The last turn's MCP server status of a chat (session.mcp_status, from agents.mcp_status_changed).
const MCP_STATUS_LABELS = { connected: 'connected', failed: 'failed', skipped: 'skipped', off: 'off in this chat', disabled: 'disabled' }
function mcpStatusText(status) {
  const counts = {}
  for (const item of status) counts[item.status] = (counts[item.status] || 0) + 1
  return Object.keys(MCP_STATUS_LABELS).filter(key => counts[key]).map(key => `${counts[key]} ${MCP_STATUS_LABELS[key]}`).join(' · ')
}

function subagentsText(children) {
  if (!children.length) return 'None yet'
  const running = children.filter(child => child.status && child.status !== 'idle').length
  return `${running.toLocaleString('en-US')} running · ${(children.length - running).toLocaleString('en-US')} finished`
}

function extensionsText({ servers = [], hooks = [] }) {
  if (!servers.length && !hooks.length) return 'None configured'
  const on = items => items.filter(item => item.enabled !== false).length
  return [servers.length ? `${plural(servers.length, 'MCP server')} (${on(servers)} on)` : '',
    hooks.length ? `${plural(hooks.length, 'hook')} (${on(hooks)} on)` : ''].filter(Boolean).join(' · ')
}

function Overview({ session, workspace, changedFiles, onOpen }) {
  const data = useOverview(session)
  const value = (key, show) => data[key]?.failed ? 'Could not load' : data[key] ? show(data[key].value) : 'Loading…'
  const chat = session ? [
    { label: 'Model', text: modelLabel(session.model) === session.model ? session.model : `${modelLabel(session.model)} · ${session.model}` },
    ...(typeof session.reasoning_effort === 'string' && EFFORT_LABELS[session.reasoning_effort]
      ? [{ label: 'Reasoning effort', text: EFFORT_LABELS[session.reasoning_effort] }] : []),
    { label: 'Permission mode', text: modeLabel(session.permission_mode || 'manual') },
    { label: 'Tools', tab: 'Tools', text: value('tools', toolsText) },
    ...(Array.isArray(session.mcp_status) && session.mcp_status.length
      ? [{ label: 'MCP servers', tab: 'Extensions', text: `Last turn: ${mcpStatusText(session.mcp_status)}` }] : []),
    { label: 'Subagents', tab: 'Subagents', text: value('children', subagentsText) },
    { label: 'Tasks', tab: 'Tasks', text: value('tasks', tasks => tasks.length
      ? `${tasks.filter(task => task.status === 'completed').length} of ${tasks.length} done` : 'None yet') },
    ...(typeof changedFiles === 'number' ? [{ label: 'Unreviewed changes', text: changedFiles ? plural(changedFiles, 'file') : 'None' }] : []),
    { label: 'Usage', tab: 'Usage', text: typeof session.spending_limit_dbu === 'number'
      ? `Spending limit: ${session.spending_limit_dbu ? `${session.spending_limit_dbu} DBU` : 'none'} (this chat)`
      : 'Model calls, tokens, estimated cost and spending limit' },
    { label: 'Export', tab: 'Export', text: 'Markdown transcript, conversation file, fork' },
  ] : []
  const project = [
    { label: 'Checkpoints', tab: 'Checkpoints', text: value('checkpoints', items => items.length ? plural(items.length, 'file checkpoint') : 'None yet') },
    { label: 'Verify', tab: 'Verify', text: value('project', settings => settings.verify_command ? html`<code>${settings.verify_command}</code>` : 'Off') },
    { label: 'Worktrees', tab: 'Worktrees', text: value('worktrees', items => items.length ? plural(items.length, 'managed worktree') : 'None') },
    { label: 'Extensions', tab: 'Extensions', text: value('extensions', extensionsText) },
  ]
  const row = item => html`<div class="overview-row" key=${item.label}><dt>${item.label}</dt><dd><span class="overview-value">${item.text}</span>
    ${item.tab ? html`<button class="outline" aria-label=${`Open ${item.tab}`} onClick=${() => onOpen(item.tab)}>Open</button>` : null}</dd></div>`
  const folder = data.project?.value?.workspace || session?.workspace || workspace || ''
  return html`<section class="agent-tools-section overview-panel" aria-label="Overview">
    <h3>This chat</h3>
    ${session ? html`<dl class="overview-list" aria-label="This chat">${chat.map(row)}</dl>`
      : html`<p>Start a conversation to see its tools, tasks, subagents and usage here.</p>`}
    <h3>Project</h3>
    ${folder ? html`<p class="field-help overview-folder">${folder}</p>` : null}
    <dl class="overview-list" aria-label="Project">${project.map(row)}</dl>
  </section>`
}

// File checkpoints: preview and restore by user turn or by file.
function Checkpoints({ session, onError, onSelectSession }) {
  const query = scopeQuery(session)
  const { busy, error, blocker, run } = useRequest(onError)
  const [items, setItems] = useState([])
  const [previews, setPreviews] = useState([])
  const [turnPreview, setTurnPreview] = useState(null)
  const [notice, setNotice] = useState('')
  const refresh = useCallback(() => run(() => api(`/checkpoints${query}`), setItems), [query, run])
  useEffect(() => { void refresh() }, [refresh])
  const groups = new Map()
  for (const item of items) {
    const key = item.turn_id || ''
    groups.set(key, [...(groups.get(key) || []), item])
  }
  const previewTurn = (turnId, offset = 0) => {
    setNotice('')
    if (!offset) { setPreviews([]); setTurnPreview(null) }
    void run(() => api(`/checkpoint-turns/${encodeURIComponent(turnId)}/preview${query ? `${query}&` : '?'}offset=${offset}`), result => {
      setTurnPreview(result)
      setPreviews(current => offset ? [...current, ...result.previews] : result.previews)
    })
  }
  return html`<section class="agent-tools-section" aria-label="Checkpoints">
    <div class="agent-tools-section-heading"><h3>File checkpoints</h3><button class="outline" disabled=${busy} onClick=${() => void refresh()}>Refresh checkpoints</button></div>
    <p>Preview file checkpoints by user turn, then restore individual files. Commands and MCP side effects are not reversible here.</p>
    ${error ? html`<p class=${`form-error ${blocker ? 'blocked-error' : ''}`} role="alert"><span>${error}</span>
      <${OpenBlockingChat} blocker=${blocker} currentId=${session?.id} onSelect=${onSelectSession} /></p>` : null}
    ${notice ? html`<p role="status">${notice}</p>` : null}
    <div class="agent-tools-list">${Array.from(groups, ([turnId, checkpoints]) => html`<section class="recovery-turn" key=${turnId || 'unassociated'}>
      <div class="agent-tools-section-heading"><div><h4>${turnId ? 'User turn' : 'Other file edits'}</h4>
        ${turnId ? html`<p>${session?.events.find(event => event.id === turnId && event.type === 'user')?.text?.slice(0, 160) || `Turn ${turnId.slice(0, 8)}`}</p>` : html`<p>Manual edits, restores, or older checkpoints without turn metadata.</p>`}</div>
        ${turnId ? html`<button class="outline" disabled=${busy} aria-label=${`Preview turn ${turnId}`} onClick=${() => previewTurn(turnId)}>Preview turn</button>` : null}</div>
      ${checkpoints.map(item => html`<div class="agent-tools-row" key=${item.id}><div><strong>${item.path}</strong><small>${item.status}</small></div>
        <button class="outline" disabled=${busy} aria-label=${`Preview checkpoint ${item.path}`} onClick=${() => { setNotice(''); setPreviews([]); setTurnPreview(null); void run(() => api(`/checkpoints/${encodeURIComponent(item.id)}/preview${query}`), result => setPreviews([result])) }}>Preview</button></div>`)}
    </section>`)}</div>
    ${!busy && !items.length ? html`<p class="muted">No saved file checkpoints in this workspace scope.</p>` : null}
    ${turnPreview ? html`<p>Individual checkpoints, newest first. Earlier edits to the same file may no longer be restorable. This is not a whole-turn undo.</p>` : null}
    ${previews.map(preview => html`<div class="agent-tools-preview" key=${preview.id} role="group" aria-label=${`Recovery preview ${preview.path}`}><h4>${preview.path}</h4><pre>${preview.diff || 'No content difference.'}</pre>
      ${preview.error ? html`<p role="alert">${preview.error}</p>` : null}
      <button class="primary" disabled=${busy || !preview.can_restore} onClick=${() => void run(async () => {
        await api(`/checkpoints/${encodeURIComponent(preview.id)}/restore${query}`, 'POST', { expected_current_hash: preview.expected_current_hash })
        return api(`/checkpoints${query}`)
      }, items => { setItems(items); setNotice(`Restored ${preview.path}.`); setPreviews([]); setTurnPreview(null) })}>Restore checkpoint</button>
    </div>`)}
    ${turnPreview?.next_offset != null ? html`<button class="outline" disabled=${busy} onClick=${() => previewTurn(turnPreview.turn_id, turnPreview.next_offset)}>More checkpoints in this turn</button>` : null}
  </section>`
}

function Worktrees({ session, onError, onSelectSession, onClose }) {
  const query = scopeQuery(session)
  const { busy, error, run } = useRequest(onError)
  const [items, setItems] = useState([])
  const [branch, setBranch] = useState('')
  const refresh = useCallback(() => run(() => api(`/worktrees${query}`), setItems), [query, run])
  useEffect(() => { void refresh() }, [refresh])
  return html`<section class="agent-tools-section" aria-label="Worktrees">
    <div class="agent-tools-section-heading"><h3>Isolated worktrees</h3><button class="outline" disabled=${busy} onClick=${() => void refresh()}>Refresh worktrees</button></div>
    <p>Create a fresh branch from committed HEAD. Uncommitted changes are not copied.</p>
    <form class="agent-tools-form" onSubmit=${e => { e.preventDefault(); void run(async () => {
      await api(`/worktrees${query}`, 'POST', { branch: branch.trim() })
      return api(`/worktrees${query}`)
    }, items => { setItems(items); setBranch('') }) }}><label>New branch<input required disabled=${busy} value=${branch} onInput=${e => setBranch(e.currentTarget.value)} placeholder="feature/my-change" /></label><button class="primary" disabled=${busy || !branch.trim()}>Create worktree</button></form>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    <div class="agent-tools-list">${items.map(item => html`<div class="agent-tools-row" key=${item.id}><div><strong>${item.branch}</strong><small>${item.path}</small></div><div class="agent-tools-actions">
      <button class="outline" disabled=${busy} aria-label=${`Open conversation in ${item.branch}`} onClick=${() => void run(() => api(`/worktrees/${encodeURIComponent(item.id)}/session${query}`, 'POST'), created => { onSelectSession(created.id); onClose() })}>Open conversation</button>
      <button class="outline" disabled=${busy} aria-label=${`Remove worktree ${item.branch}`} onClick=${() => void run(async () => { await api(`/worktrees/${encodeURIComponent(item.id)}${query}`, 'DELETE'); return api(`/worktrees${query}`) }, setItems)}>Remove</button>
    </div></div>`)}</div>
    ${!busy && !items.length ? html`<p class="muted">No managed worktrees in this workspace scope.</p>` : null}
    <p class="field-help">Removing a worktree preserves its branch. Worktrees with uncommitted changes or conversations in use cannot be removed.</p>
  </section>`
}


function Tasks(props) {
  return props.session ? html`<${SessionTasks} ...${props} />` : html`<section class="agent-tools-section" aria-label="Tasks"><p>Start a conversation to manage its tasks.</p></section>`
}

function SessionTasks({ session, onError }) {
  const path = `/sessions/${encodeURIComponent(session.id)}`
  const { busy, error, run } = useRequest(onError)
  const [tasks, setTasks] = useState([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [dependencies, setDependencies] = useState([])
  const refresh = useCallback(() => run(() => api(`${path}/tasks`), setTasks), [path, run])
  useEffect(() => { void refresh() }, [refresh])
  const update = (task, status) => void run(() => api(`${path}/tasks/${encodeURIComponent(task.id)}`, 'PATCH', { status }), updated => setTasks(current => current.map(item => item.id === updated.id ? updated : item)))
  const done = tasks.filter(task => task.status === 'completed').length
  return html`<section class="agent-tools-section" aria-label="Tasks">
    <div class="agent-tools-section-heading"><h3>Conversation tasks</h3><button class="outline" disabled=${busy} onClick=${() => void refresh()}>Refresh tasks</button></div>
    <p>The agent records multi-step work here with create_task and update_task; the checklist above the message box shows the same tasks.${tasks.length ? ` ${done} of ${tasks.length} done.` : ''}</p>
    <form class="agent-tools-form" onSubmit=${e => { e.preventDefault(); void run(() => api(`${path}/tasks`, 'POST', { title: title.trim(), description, depends_on: dependencies }), task => { setTasks(current => [...current, task]); setTitle(''); setDescription(''); setDependencies([]) }) }}>
      <label>Task title<input required disabled=${busy} maxLength=${200} value=${title} onInput=${e => setTitle(e.currentTarget.value)} /></label>
      <label>Task description<textarea disabled=${busy} maxLength=${4000} value=${description} onInput=${e => setDescription(e.currentTarget.value)} /></label>
      ${tasks.length ? html`<details class="agent-tools-dependencies"><summary>Dependencies</summary>${tasks.map(task => html`<label key=${task.id}><input type="checkbox" disabled=${busy} checked=${dependencies.includes(task.id)} onChange=${e => { const checked = e.currentTarget.checked; setDependencies(current => checked ? [...current, task.id] : current.filter(id => id !== task.id)) }} />${task.title}</label>`)}</details>` : null}
      <button class="primary" disabled=${busy || !title.trim() || tasks.length >= 50}>Add task</button>
    </form>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    <div class="agent-tools-list">${tasks.map(task => html`<div class="agent-tools-row" key=${task.id}><div><label class="agent-tools-task"><input type="checkbox" aria-label=${`Complete task ${task.title}`} disabled=${busy} checked=${task.status === 'completed'} onChange=${e => update(task, e.currentTarget.checked ? 'completed' : 'pending')} /><strong>${task.title}</strong></label>
      ${task.description ? html`<p>${task.description}</p>` : null}${task.depends_on.length ? html`<small>Depends on: ${task.depends_on.map(id => tasks.find(item => item.id === id)?.title || id).join(', ')}</small>` : null}</div>
      <select aria-label=${`Status for ${task.title}`} disabled=${busy} value=${task.status} onChange=${e => update(task, e.currentTarget.value)}>${taskStatuses.map(status => html`<option key=${status} value=${status}>${status.replace('_', ' ')}</option>`)}</select>
    </div>`)}</div>
    ${!busy && !tasks.length ? html`<p class="muted">No tasks in this conversation yet.</p>` : null}
  </section>`
}

function Subagents(props) {
  return props.session ? html`<${SessionSubagents} ...${props} />` : html`<section class="agent-tools-section" aria-label="Subagents"><p>Start a conversation to see its subagents.</p></section>`
}

// A child's status: what it is doing now, else how its delegation ended.
function childStatus(child) {
  if (child.status === 'awaiting_approval') return 'Waiting for approval'
  if (child.status && child.status !== 'idle') return child.status.replace(/_/g, ' ')
  return String(child.delegation?.status || child.subagent?.status || 'idle').replace(/_/g, ' ')
}

const moment = seconds => new Date(seconds * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

function SubagentRow({ child, onOpen }) {
  const entry = child.subagent || {}
  const progress = child.delegation || {}
  const kind = child.agent_type || entry.agent_type || progress.agent_type
  // The effort and model it started with; an older child shows its own header's.
  const settings = agentSettingsText(progress.effort || entry.effort || child.reasoning_effort, progress.model || entry.model || child.model)
  const started = typeof progress.started === 'number' ? progress.started : typeof entry.created === 'number' ? entry.created : child.created
  const ended = typeof progress.ended === 'number' ? progress.ended : typeof entry.ended === 'number' ? entry.ended : null
  const work = [Number.isInteger(entry.steps_used) ? `${entry.steps_used} of ${child.max_steps ?? '?'} steps`
    : Number.isInteger(child.max_steps) ? `up to ${child.max_steps} steps` : '',
  Number.isInteger(entry.tool_calls) ? plural(entry.tool_calls, 'tool call')
    : Number.isInteger(progress.completed_tools) ? plural(progress.completed_tools, 'completed action') : '']
  return html`<div class="agent-tools-row subagent-row"><div>
    <strong>${entry.task || child.title}</strong>
    <small>${[agentLabel(kind), childStatus(child), profileLabels[child.tool_profile || 'inherit'], settings].filter(Boolean).join(' · ')}</small>
    <small>${work.filter(Boolean).join(' · ')}</small>
    ${typeof started === 'number' ? html`<small>Started ${moment(started)}${ended != null ? ` · ended ${moment(ended)} (${elapsedText(ended - started)})` : ''}</small>` : null}
    <small class="subagent-transcript" title="In the app's state folder">Transcript: <code>${child.transcript || `conversations/${child.id}.jsonl`}</code></small>
  </div><button class="outline" aria-label=${`Open subagent ${child.title}`} onClick=${onOpen}>Open</button></div>`
}

function SessionSubagents({ session, onError, onSelectSession, onClose }) {
  const path = `/sessions/${encodeURIComponent(session.id)}`
  const { busy, error, run } = useRequest(onError)
  const [children, setChildren] = useState([])
  const [profile, setProfile] = useState(session.subagent_tool_profile || 'inherit')
  const [profileNotice, setProfileNotice] = useState('')
  const refresh = useCallback(() => run(() => api(`${path}/children`), setChildren), [path, run])
  useEffect(() => { void refresh() }, [refresh])
  return html`<section class="agent-tools-section" aria-label="Subagents">
    <div class="agent-tools-section-heading"><h3>Subagents of this chat</h3><button class="outline" disabled=${busy} onClick=${() => void refresh()}>Refresh subagents</button></div>
    <p>The agent hands self-contained work (exploring code, wide searches, reading many files, summarizing a long command) to subagents, several at once when the work is independent, so only their reports enter this chat's context. Each subagent is a chat of its own: open it to follow its work or approve its actions.</p>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    <div class="agent-tools-list">${children.map(child => html`<${SubagentRow} key=${child.id} child=${child} onOpen=${() => { onSelectSession(child.id); onClose() }} />`)}</div>
    ${!busy && !children.length ? html`<p class="muted">No subagents in this conversation.</p>` : null}
    ${session.is_subagent ? null : html`<${AgentTypes} session=${session} onError=${onError} />`}
    <h3>Tool ceiling</h3>
    ${session.is_subagent ? html`<p>Tool profile: ${profileLabels[session.tool_profile || 'inherit']}. This child cannot widen its inherited tool access.</p>` : html`
      <label class="agent-tools-profile">New subagent tool ceiling<select aria-label="New subagent tool ceiling" disabled=${busy || session.status !== 'idle'} value=${profile} onChange=${e => {
        const selected = e.currentTarget.value
        setProfileNotice('')
        void run(() => api(`${path}/subagent-profile`, 'PUT', { tool_profile: selected }), updated => { setProfile(updated.subagent_tool_profile || 'inherit'); setProfileNotice('Tool ceiling saved for new subagents.') })
      }}>${Object.entries(profileLabels).map(([value, label]) => html`<option key=${value} value=${value}>${label}</option>`)}</select></label>
      <p class="field-help">The widest tools a new subagent of this chat gets: explore subagents are read-only and editor subagents edit files without commands, but neither gets more than this. File-only profiles disable commands, MCP and hooks; normal approvals still apply. Applies to new subagents only, and limits agent tools, not your editor or terminal.</p>
      ${profileNotice ? html`<p role="status">${profileNotice}</p>` : null}`}
  </section>`
}

// The agent types delegate_task offers in this chat's project (GET /agents): the built-in ones with their
// Settings → Subagents defaults, the project's custom agent files, and the files that cannot be used.
function AgentTypes({ session, onError }) {
  const { busy, error, run } = useRequest(onError)
  const [agents, setAgents] = useState(null)
  const query = scopeQuery(session)
  const refresh = useCallback(() => run(() => api(`/agents${query}`), setAgents), [query, run])
  useEffect(() => { void refresh() }, [refresh])
  const effort = value => value ? `${EFFORT_LABELS[value] || value} effort` : 'Same effort as the chat'
  const model = value => value ? modelLabel(value) : 'Same model as the chat'
  const builtIn = agents?.built_in || []
  const custom = agents?.custom || []
  const errors = agents?.errors || []
  return html`<div class="agent-types">
    <div class="agent-tools-section-heading"><h3>Agent types</h3><button class="outline" disabled=${busy} onClick=${() => void refresh()}>Refresh agent types</button></div>
    <p>The agent chooses a type for each subagent. Set the built-in types' model and effort in Settings → Subagents. Custom agents are Markdown files in this project's <code>.agents/agents</code> or <code>.claude/agents</code> folder; their tools never go beyond this chat's tool ceiling.</p>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    ${agents ? html`<ul class="agent-types-list" aria-label="Agent types">
      ${builtIn.map(agent => html`<li key=${agent.name} class="agent-type-row">
        <div class="agent-type-title"><strong>${agentLabel(agent.name)}</strong><span class="agent-type-badge">Built-in</span></div>
        <small>${agent.description}</small>
        <small>${[profileLabels[agent.profile], effort(agent.effort), model(agent.model)].join(' · ')}</small>
      </li>`)}
      ${custom.map(agent => html`<li key=${agent.path} class="agent-type-row">
        <div class="agent-type-title"><strong>${agent.name}</strong><span class="agent-type-badge">Custom</span></div>
        <small>${agent.description}</small>
        <small>${[profileLabels[agent.profile], Array.isArray(agent.tools) ? `Tools: ${agent.tools.join(', ')}` : 'Every tool of its profile',
          effort(agent.effort), model(agent.model), Number.isInteger(agent.max_steps) ? `${agent.max_steps} steps by default` : '',
          agent.skills?.length ? `Skills: ${agent.skills.join(', ')}` : ''].filter(Boolean).join(' · ')}</small>
        <small class="agent-type-file">File: <code>${agent.path}</code></small>
        ${(agent.notes || []).map((note, index) => html`<small key=${index} class="agent-type-note">${note}</small>`)}
      </li>`)}
      ${errors.map(item => html`<li key=${item.path} class="agent-type-row agent-type-error">
        <div class="agent-type-title"><strong><code>${item.path}</code></strong><span class="agent-type-badge">Not used</span></div>
        <p class="form-error">${item.error}</p>
      </li>`)}
    </ul>` : null}
    ${agents && !custom.length && !errors.length ? html`<p class="muted">No custom agents in this project.</p>` : null}
  </div>`
}

// The project's Verify command (app state, keyed by the resolved workspace, so a worktree is its own project).
function Verify({ session, workspace, onError }) {
  const query = scopeQuery(session)
  const { busy, error, run } = useRequest(onError)
  const [project, setProject] = useState(null)
  const [command, setCommand] = useState('')
  const [seconds, setSeconds] = useState('600')
  const [rounds, setRounds] = useState('2')
  const [notice, setNotice] = useState('')
  const show = useCallback(values => {
    setProject(values); setCommand(values.verify_command)
    setSeconds(String(values.verify_timeout_seconds)); setRounds(String(values.verify_max_rounds))
  }, [])
  useEffect(() => { void run(() => api(`/project-settings${query}`), show) }, [query, run, show])
  const save = e => {
    e.preventDefault()
    if (!project || busy) return  // A repeat Enter or click while saving.
    setNotice('')
    void run(() => api(`/project-settings${query}`, 'PUT', { verify_command: command.trim(), verify_timeout_seconds: Number(seconds), verify_max_rounds: Number(rounds) }),
      values => { show(values); setNotice(values.verify_command ? 'Verify command saved for this project.' : 'Verify is off for this project.') })
  }
  const folder = project?.workspace || session?.workspace || workspace || 'this workspace'
  return html`<section class="agent-tools-section" aria-label="Verify">
    <h3>Verify command</h3>
    <p>Runs after a reply in which the agent changed files in this project (${folder}). It runs like the agent's run_command, so the permission mode and rules apply: it asks for approval unless the mode or an allow rule (for this chat, this project or all projects) allows it, deny rules block it, Don't ask blocks it unless a rule allows it, and it never runs in Plan mode. If it fails, the agent sees the end of its output and gets another model request to fix it, up to the fix rounds below. Leave it empty to turn it off.</p>
    <form class="agent-tools-form" onSubmit=${save}>
      <label>Command<input disabled=${!project} readOnly=${busy} maxLength=${2000} spellcheck=${false} value=${command} placeholder="pytest -q" onInput=${e => setCommand(e.currentTarget.value)} /></label>
      <label>Time limit (seconds)<input type="number" min="10" max="3600" step="1" required disabled=${!project} readOnly=${busy} value=${seconds} onInput=${e => setSeconds(e.currentTarget.value)} /></label>
      <label>Fix rounds<input type="number" min="1" max="3" step="1" required disabled=${!project} readOnly=${busy} value=${rounds} onInput=${e => setRounds(e.currentTarget.value)} /></label>
      <div class="agent-tools-actions"><button class="primary" disabled=${!project} aria-disabled=${busy ? 'true' : 'false'}>Save</button></div>
    </form>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}${notice ? html`<p role="status">${notice}</p>` : null}
    <p class="field-help">Saved in this app's state for this project folder, not in the repository, and not included in conversation exports. Each fix round uses agent steps; the command card shows the full output.</p>
  </section>`
}

const extensionExample = JSON.stringify({ servers: [
  { id: 'example', name: 'Example MCP', transport: 'stdio', command: 'your-mcp-server', args: [], env: { API_TOKEN: { secret: 'EXAMPLE_TOKEN' } }, enabled: false },
  { id: 'remote', name: 'Remote MCP', transport: 'http', url: 'https://mcp.example.com/mcp', headers: { Authorization: { secret: 'REMOTE_TOKEN', prefix: 'Bearer ' } }, enabled: false },
  { id: 'workspace-sql', name: 'Databricks SQL', transport: 'http', url: 'https://your-workspace.cloud.databricks.com/api/2.0/mcp/sql', workspace_credentials: true, enabled: false }], hooks: [
  { id: 'example-hook', event: 'before_tool', tools: ['write_file', 'edit_file'], command: 'your-hook-command', timeout_seconds: 10, enabled: false },
  { id: 'prompt-check', event: 'user_prompt', command: 'your-prompt-check', timeout_seconds: 10, enabled: false },
  { id: 'stop-check', event: 'agent_stop', command: 'your-stop-check', timeout_seconds: 30, enabled: false }] }, null, 2)
// Hook events (extensions.HOOK_EVENTS): when each runs and the JSON output fields it reads.
const HOOK_EVENT_HELP = [
  ['before_tool', 'Before a tool call, before its approval card', 'decision "deny" (with reason) or "ask"; arguments (a replacement, checked again); context'],
  ['after_tool', 'After a tool call completed', 'context'],
  ['tool_failure', 'After a tool call failed', 'context'],
  ['user_prompt', 'Before your message goes to the agent', 'decision "block" (with reason); prompt (a replacement); context'],
  ['session_start', "Before a chat's first model request", 'context'],
  ['agent_stop', 'When the agent would end its turn', 'decision "block" (with reason): it keeps working, at most 3 times'],
  ['subagent_start', "Before a subagent's first request", 'context'],
  ['subagent_stop', 'When a subagent would finish', 'decision "block" (with reason): one more round, at most 2; report (a replacement)'],
]
const hookOutputExample = JSON.stringify({ decision: 'deny', reason: 'Generated files are rebuilt by make; edit the template.' })

// A server's line in the last-turn status or the test results.
function serverLine(server, test) {
  const info = server.server_info ? [server.server_info.title || server.server_info.name, server.server_info.version].filter(Boolean).join(' ') : ''
  const count = Number.isInteger(server.tool_count) ? server.tool_count : (server.tools || []).length
  const state = server.status === 'connected' ? `${test ? 'Connected during test' : 'Connected'} · ${plural(count, 'tool')}`
    : server.status === 'disabled' ? 'Disabled · not started' : server.status === 'off' ? 'Turned off in this chat · not started'
      : server.status === 'skipped' ? 'Skipped' : 'Failed'
  return [state, info ? `server: ${info}` : '', server.retry_seconds ? `tried again in about ${server.retry_seconds} s` : ''].filter(Boolean).join(' · ')
}

function ServerRow({ server, test }) {
  const hints = server.tool_annotations || {}
  return html`<div class="agent-tools-row" key=${server.id}><div><strong>${server.name}</strong><small>${serverLine(server, test)}</small>
    ${server.error ? html`<p class="form-error">${server.error}</p>` : null}
    ${server.capped?.length ? html`<p>Not offered (Databricks takes at most 32 tools per request): ${server.capped.join(', ')}</p>` : null}
    ${server.stderr_tail ? html`<details class="mcp-stderr"><summary>Server stderr (end)</summary><pre>${server.stderr_tail}</pre></details>` : null}
    ${server.instructions ? html`<details class="mcp-stderr"><summary>Server instructions (shown here only; never sent to the agent)</summary><pre>${server.instructions}</pre></details>` : null}
    ${test && server.tools?.length ? html`<ul class="mcp-tools">${server.tools.map(name => html`<li key=${name}><code>${name}</code>${hintText(hints[name]) ? html` <small>Server says: ${hintText(hints[name])}</small>` : null}</li>`)}</ul>` : null}
  </div></div>`
}

function ExtensionsPanel({ session, onError }) {
  const query = scopeQuery(session)
  const { busy, error, run } = useRequest(onError)
  const [config, setConfig] = useState(null)
  const [skills, setSkills] = useState([])
  const [activeSkills, setActiveSkills] = useState(session?.active_skills || [])
  const [notice, setNotice] = useState('')
  const [tools, setTools] = useState(null)
  const [servers, setServers] = useState(null)
  useEffect(() => { void run(() => Promise.all([api('/extensions'), api(`/skills${query}`)]), ([config, skills]) => { setConfig(config); setSkills(skills) }) }, [query, run])
  const status = Array.isArray(session?.mcp_status) ? session.mcp_status : []
  const save = draft => { setNotice(''); setTools(null); setServers(null); void run(() => api('/extensions', 'PUT', draft), saved => { setConfig(saved); setNotice('Configuration saved.') }) }
  const test = html`<button type="button" class="outline" disabled=${busy} onClick=${() => { setNotice(''); setTools(null); setServers(null); void run(() => api('/extensions/test', 'POST'), result => { setTools(result.tools); setServers(result.servers || null) }) }}>Test saved configuration</button>`
  return html`<section class="agent-tools-section" aria-label="Extensions">
    <h3>MCP servers and hooks</h3>
    <p>Configure stdio commands or HTTP server URLs, and up to 16 hooks: local commands that run around tool calls and at the start and end of a turn. Saving authorizes enabled servers to start. A server that fails is skipped (its tools are unavailable, the others work) and named once in the chat.</p>
    <${ExtensionsEditor} config=${config} busy=${busy} session=${session} onSave=${save} onError=${onError} actions=${test} />
    <details class="agent-tools-example"><summary>Configuration example</summary><pre>${extensionExample}</pre><p>The optional tools list matches exact tool names for before_tool, after_tool and tool_failure hooks (other events ignore it). Omit it to match all tools; an empty list matches none. Tool hooks' input includes call_id; failure hooks also receive the original error.</p><p>{"secret": "NAME"} reads NAME from your credential file each time the server starts; the value is never saved in this configuration. workspace_credentials sends your Databricks token only to an https URL on your workspace host.</p></details>
    <details class="agent-tools-example hook-events-help"><summary>Hook events and JSON output</summary>
      <div class="hook-events-scroll"><table class="hook-events" aria-label="Hook events"><thead><tr><th scope="col">Event</th><th scope="col">Runs</th><th scope="col">JSON output it reads</th></tr></thead>
        <tbody>${HOOK_EVENT_HELP.map(([event, when, fields]) => html`<tr key=${event}><th scope="row"><code>${event}</code></th><td>${when}</td><td>${fields}</td></tr>`)}</tbody></table></div>
      <p>A hook gets a JSON object on stdin (the event, the chat and, per event, the tool call, your message, the reply or the subagent's task). It may print one JSON object on stdout, for example <code>${hookOutputExample}</code>; other output is plain text, as before, but output in braces that is not valid JSON fails the hook. stderr is shown on the hook card, never read as JSON. Exit code 0 means no objection; another exit code blocks a before_tool call and only warns for other events.</p>
      <p>Context (up to 2,000 characters per hook) reaches the agent labelled as hook output. Replaced arguments are checked again against the tool's schema, your rules and its approval card. "allow" never skips a card the mode or rules need; "ask" always shows one, also in Bypass permissions (Don't ask refuses the call instead). Each hook still asks for approval except in Bypass, rules can allow or block it, and Plan mode skips hooks.</p></details>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}${notice ? html`<p role="status">${notice}</p>` : null}
    ${tools ? html`<div class="agent-tools-test" role="status">${tools.length ? html`<strong>Available tools</strong><ul>${tools.map(tool => html`<li key=${tool}>${tool}</li>`)}</ul>` : 'No tools reported by the saved configuration.'}</div>` : null}
    ${servers ? html`<div class="agent-tools-list" aria-label="MCP server test results"><p class="field-help">Results from the last test, not a live connection monitor.</p>${servers.map(server => html`<${ServerRow} key=${server.id} server=${server} test=${true} />`)}</div>` : null}
    ${status.length ? html`<h3>Last turn in this chat</h3><div class="agent-tools-list" aria-label="MCP servers in the last turn">${status.map(server => html`<${ServerRow} key=${server.id} server=${server} test=${false} />`)}</div>` : null}
    <h3>Skills</h3>${!session ? html`<p class="field-help">Start a conversation to enable skills.</p>` : null}
    <div class="agent-tools-list">${skills.map(skill => html`<div class="agent-tools-row" key=${skill.id}><label><input type="checkbox" disabled=${busy || !session} checked=${activeSkills.includes(skill.id)} onChange=${e => { if (session) void run(() => api(`/sessions/${encodeURIComponent(session.id)}/skills`, 'POST', { skill_id: skill.id, enabled: e.currentTarget.checked }), result => setActiveSkills(result.active_skills)) }} />${skill.name}<small>${skill.description}</small><small>${skill.path}</small></label></div>`)}</div>
    ${!busy && !skills.length ? html`<p class="muted">No installed skills found.</p>` : null}
  </section>`
}


// MCP tool names the server accepts in a chat's turned-off list, and a whole server's switch.
const MCP_TOOL = /^mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_.-]+$/
const MCP_SERVER_SWITCH = /^mcp__[a-z][a-z0-9_-]{0,19}__\*$/
const serverSwitch = id => `mcp__${id}__*`

// The Tools section's groups; a tool in none of them is under Other.
const TOOL_GROUPS = [
  { name: 'Files', tools: ['list_files', 'read_file', 'outline_file', 'write_file', 'edit_file', 'move_path', 'delete_path',
    'notebook_read', 'notebook_edit', 'insert_activity_log'] },
  { name: 'Search', tools: ['find_files', 'search_files', 'project_overview'] },
  { name: 'Commands and jobs', tools: ['run_command', 'list_jobs', 'get_job_output', 'wait_job', 'stop_job'] },
  { name: 'Git', match: name => /^git_/.test(name) },
  { name: 'Tasks and subagents', tools: ['list_tasks', 'get_task', 'create_task', 'update_task', 'delegate_task'] },
  { name: 'Skills', tools: ['list_skills', 'use_skill'] },
  { name: 'MCP', tools: ['get_tool_output'] },
]

export function toolGroup(name) {
  return TOOL_GROUPS.find(group => group.tools?.includes(name) || group.match?.(name))?.name || 'Other'
}

// The first sentence of a tool's description; the whole (shortened by the server) shows on hover.
const firstSentence = text => /^(.+?[.!?])(?:\s|$)/.exec(text || '')?.[1] || text || ''

// Turned-off tools of this chat: GET /tools?session_id= lists the built-in and feature tools.
function ToolsPanel({ session, onError, onSaveTools }) {
  const { busy, error, run } = useRequest(onError)
  const [tools, setTools] = useState(null)
  const [servers, setServers] = useState([])
  const [off, setOff] = useState([])
  const [filter, setFilter] = useState('')
  const [mcpName, setMcpName] = useState('')
  const [mcpError, setMcpError] = useState('')
  const [notice, setNotice] = useState('')
  const id = session?.id
  useEffect(() => {
    if (id) void run(() => api(`/tools?session_id=${encodeURIComponent(id)}`), result => { setTools(result.tools || []); setServers(result.servers || []); setOff(result.disabled || []) })
  }, [id, run])
  if (!session) return html`<section class="agent-tools-section" aria-label="Tools"><p>Start a conversation to choose its tools.</p></section>`
  const names = new Set((tools || []).map(tool => tool.name))
  const switches = new Set(servers.map(server => serverSwitch(server.id)))
  const extra = off.filter(name => !names.has(name) && !switches.has(name))
  const toggle = (name, available) => { setNotice(''); setOff(current => available ? current.filter(item => item !== name) : [...current, name]) }
  const setGroup = (group, available) => {
    setNotice('')
    const members = group.tools.map(tool => tool.name)
    setOff(current => available ? current.filter(item => !members.includes(item)) : [...current, ...members.filter(item => !current.includes(item))])
  }
  const query = filter.trim().toLowerCase()
  const shown = (tools || []).filter(tool => !query || [tool.name, tool.description || '', toolGroup(tool.name)]
    .some(text => text.toLowerCase().includes(query)))
  const groups = [...TOOL_GROUPS.map(group => group.name), 'Other']
    .map(name => ({ name, tools: shown.filter(tool => toolGroup(tool.name) === name) })).filter(group => group.tools.length)
  const enabled = (tools || []).filter(tool => !off.includes(tool.name)).length
  const addMcp = () => {
    const name = mcpName.trim()
    if (!MCP_TOOL.test(name)) { setMcpError('Enter an MCP tool name such as mcp__docs__search.'); return }
    setMcpError(''); setNotice(''); setMcpName('')
    setOff(current => current.includes(name) ? current : [...current, name])
  }
  const save = () => {
    setNotice('')
    const operation = onSaveTools ? () => onSaveTools(off) : () => api(`/sessions/${encodeURIComponent(id)}/disabled-tools`, 'PUT', { tools: off })
    void run(operation, updated => { if (Array.isArray(updated?.disabled_tools)) setOff(updated.disabled_tools); setNotice('Tools saved for this chat.') })
  }
  return html`<section class="agent-tools-section tools-panel" aria-label="Tools">
    <h3>Tools in this chat</h3>
    <p>Turned-off tools are not offered to the agent in this chat, and calls to them are refused. Changes apply from the agent's next step. Subagents, forks and imports keep this setting.</p>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    <div class="tools-filter-row">
      <label class="tools-filter">Filter tools<input type="search" spellcheck=${false} value=${filter} placeholder="Name, group or description"
        onInput=${e => setFilter(e.currentTarget.value)} /></label>
      ${tools ? html`<span class="tools-count">${enabled} of ${tools.length} on</span>` : null}
    </div>
    ${groups.map(group => {
      const on = group.tools.filter(tool => !off.includes(tool.name)).length
      return html`<div class="tools-group" role="group" aria-label=${group.name} key=${group.name}>
        <div class="tools-group-heading"><h4>${group.name}<small>${on} of ${group.tools.length} on</small></h4>
          <div class="agent-tools-actions">
            <button class="outline" disabled=${busy || on === group.tools.length} aria-label=${`Turn on all ${group.name} tools`} onClick=${() => setGroup(group, true)}>All on</button>
            <button class="outline" disabled=${busy || on === 0} aria-label=${`Turn off all ${group.name} tools`} onClick=${() => setGroup(group, false)}>All off</button>
          </div></div>
        <div class="agent-tools-list">${group.tools.map(tool => html`<div class="agent-tools-row" key=${tool.name}><label class="tools-toggle" title=${tool.description || ''}>
          <input type="checkbox" aria-label=${tool.name} aria-describedby=${tool.description ? `tool-description-${tool.name}` : null}
            disabled=${busy} checked=${!off.includes(tool.name)} onChange=${e => toggle(tool.name, e.currentTarget.checked)} />
          <span><strong>${tool.name}</strong>${tool.description ? html`<small id=${`tool-description-${tool.name}`}>${firstSentence(tool.description)}</small>` : null}</span></label></div>`)}</div>
      </div>`
    })}
    ${tools && query && !groups.length ? html`<p class="muted">No tools match “${filter.trim()}”.</p>` : null}
    <h4 class="tools-mcp-heading">MCP tools</h4>
    ${servers.length ? html`<div class="tools-group" role="group" aria-label="MCP servers"><div class="agent-tools-list">${servers.map(server => html`<div class="agent-tools-row" key=${server.id}><label class="tools-toggle">
      <input type="checkbox" aria-label=${`Use the ${server.name} server in this chat`} disabled=${busy} checked=${!off.includes(serverSwitch(server.id))}
        onChange=${e => toggle(serverSwitch(server.id), e.currentTarget.checked)} />
      <span><strong>${server.name}</strong><small>${server.enabled ? 'Enabled in Extensions' : 'Disabled in Extensions'} · off here: its process never starts in this chat (<code>${serverSwitch(server.id)}</code>)</small></span></label></div>`)}</div></div>` : null}
    <p class="field-help">MCP tools are offered once a turn starts their server (Agent tools → Extensions). Turn a whole server off above, or one tool by its full name.</p>
    <form class="agent-tools-form" onSubmit=${e => { e.preventDefault(); addMcp() }}>
      <label>Turn off an MCP tool by name<input spellcheck=${false} disabled=${busy} value=${mcpName} placeholder="mcp__docs__search"
        onInput=${e => { setMcpName(e.currentTarget.value); setMcpError('') }} /></label>
      <button class="outline" disabled=${busy || !mcpName.trim()}>Add</button>
    </form>
    ${mcpError ? html`<p class="form-error" role="alert">${mcpError}</p>` : null}
    ${extra.length ? html`<ul class="tool-chips" aria-label="Turned-off MCP tools">${extra.map(name => html`<li key=${name}><span>${name}</span>
      <button class="icon-button" disabled=${busy} aria-label=${`Turn on ${name}`} onClick=${() => toggle(name, true)}><${X} size=${13} /></button></li>`)}</ul>` : null}
    <div class="agent-tools-actions tools-save"><button class="primary" disabled=${busy || tools === null} onClick=${save}>Save tools</button></div>
    ${notice ? html`<p role="status">${notice}</p>` : null}
  </section>`
}
