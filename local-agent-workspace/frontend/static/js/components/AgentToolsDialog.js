import { html, useCallback, useEffect, useRef, useState } from '../lib.js'
import { X } from '../icons.js'
import { api } from '../api.js'
import ConversationTransfer from './ConversationTransfer.js'
import UsagePanel from './UsagePanel.js'
import { blockerOf, OpenBlockingChat } from './BlockingChat.js'

// Props: session, workspace?, onClose, onSelectSession, onError, onSaveTools?(tools) -> Session
const profileLabels = { inherit: 'Inherit permitted tools', read_only: 'Read-only files', file_editor: 'Read and edit files' }
const taskStatuses = ['pending', 'in_progress', 'completed', 'cancelled']
const scopeQuery = session => session ? `?session_id=${encodeURIComponent(session.id)}` : ''

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
  const [tab, setTab] = useState('Recovery')
  useEffect(() => { dialog.current?.showModal() }, [])
  return html`<dialog class="agent-tools-dialog" ref=${dialog} aria-label="Agent tools" onCancel=${props.onClose} onClick=${e => { if (e.target === dialog.current) props.onClose() }}>
    <div class="agent-tools-content">
      <header class="agent-tools-header"><div><h2>Agent tools</h2><p>${props.session?.title || 'Default workspace'}</p></div><button class="icon-button" aria-label="Close agent tools" onClick=${props.onClose}><${X} size=${18} /></button></header>
      <nav class="agent-tools-tabs" aria-label="Agent tools sections">${['Recovery', 'Usage', 'Worktrees', 'Tasks', 'Verify', 'Extensions', 'Tools', 'Conversation'].map(name => html`<button key=${name} aria-pressed=${tab === name} onClick=${() => setTab(name)}>${name}</button>`)}</nav>
      ${tab === 'Recovery' ? html`<${Recovery} ...${props} />` : tab === 'Usage' ? html`<${UsagePanel} session=${props.session} onError=${props.onError} />` : tab === 'Worktrees' ? html`<${Worktrees} ...${props} />` : tab === 'Tasks' ? html`<${Tasks} ...${props} />` : tab === 'Verify' ? html`<${Verify} ...${props} />` : tab === 'Extensions' ? html`<${ExtensionsPanel} ...${props} />` : tab === 'Tools' ? html`<${ToolsPanel} ...${props} />` :
        html`<${ConversationTransfer} sessionId=${props.session?.id} workspace=${props.session?.workspace || props.workspace || ''} busy=${!!props.session && props.session.status !== 'idle'} onError=${props.onError} onSelectSession=${id => { props.onSelectSession(id); props.onClose() }} />`}
    </div>
  </dialog>`
}

function Recovery({ session, onError, onSelectSession }) {
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
    void run(() => api(`/checkpoint-turns/${encodeURIComponent(turnId)}/preview${query}&offset=${offset}`), result => {
      setTurnPreview(result)
      setPreviews(current => offset ? [...current, ...result.previews] : result.previews)
    })
  }
  return html`<section class="agent-tools-section" aria-label="Recovery">
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
    }, items => { setItems(items); setBranch('') }) }}><label>New branch<input required disabled=${busy} value=${branch} onInput=${e => setBranch(e.currentTarget.value)} placeholder="codex/my-change" /></label><button class="primary" disabled=${busy || !branch.trim()}>Create worktree</button></form>
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
  return props.session ? html`<${SessionTasks} ...${props} />` : html`<section class="agent-tools-section"><p>Start a conversation to manage tasks and subagents.</p></section>`
}

function SessionTasks({ session, onError, onSelectSession, onClose }) {
  const path = `/sessions/${encodeURIComponent(session.id)}`
  const { busy, error, run } = useRequest(onError)
  const [tasks, setTasks] = useState([])
  const [children, setChildren] = useState([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [dependencies, setDependencies] = useState([])
  const [profile, setProfile] = useState(session.subagent_tool_profile || 'inherit')
  const [profileNotice, setProfileNotice] = useState('')
  const refresh = useCallback(() => run(() => Promise.all([api(`${path}/tasks`), api(`${path}/children`)]), ([tasks, children]) => { setTasks(tasks); setChildren(children) }), [path, run])
  useEffect(() => { void refresh() }, [refresh])
  const update = (task, status) => void run(() => api(`${path}/tasks/${encodeURIComponent(task.id)}`, 'PATCH', { status }), updated => setTasks(current => current.map(item => item.id === updated.id ? updated : item)))
  return html`<section class="agent-tools-section" aria-label="Tasks">
    <div class="agent-tools-section-heading"><h3>Conversation tasks</h3><button class="outline" disabled=${busy} onClick=${() => void refresh()}>Refresh tasks and subagents</button></div>
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
    <h3>Subagents</h3><p class="field-help">Open a child conversation to review its progress or approve a pending action.</p>
    ${session.is_subagent ? html`<p>Tool profile: ${profileLabels[session.tool_profile || 'inherit']}. This child cannot widen its inherited tool access.</p>` : html`
      <label class="agent-tools-profile">New subagent tool ceiling<select aria-label="New subagent tool ceiling" disabled=${busy || session.status !== 'idle'} value=${profile} onChange=${e => {
        const selected = e.currentTarget.value
        setProfileNotice('')
        void run(() => api(`${path}/subagent-profile`, 'PUT', { tool_profile: selected }), updated => { setProfile(updated.subagent_tool_profile || 'inherit'); setProfileNotice('Tool ceiling saved for new subagents.') })
      }}>${Object.entries(profileLabels).map(([value, label]) => html`<option key=${value} value=${value}>${label}</option>`)}</select></label>
      <p class="field-help">Applies to new subagents only. File-only profiles disable commands, MCP and hooks; normal approvals still apply. This limits agent tools, not your editor or terminal.</p>
      ${profileNotice ? html`<p role="status">${profileNotice}</p>` : null}`}
    <div class="agent-tools-list">${children.map(child => html`<div class="agent-tools-row" key=${child.id}><div><strong>${child.title}</strong><small>${child.status === 'awaiting_approval' ? 'Waiting for approval' : child.status}</small><small>${profileLabels[child.tool_profile || 'inherit']}</small></div><button class="outline" aria-label=${`Open subagent ${child.title}`} onClick=${() => { onSelectSession(child.id); onClose() }}>Open</button></div>`)}</div>
    ${!busy && !children.length ? html`<p class="muted">No subagents in this conversation.</p>` : null}
  </section>`
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
    <p>Runs after a reply in which the agent changed files in this project (${folder}). It runs like the agent's run_command: it asks for approval unless the permission mode or an “Always allow this command in this chat” rule allows it, and never runs in Plan mode. If it fails, the agent sees the end of its output and gets another model request to fix it, up to the fix rounds below. Leave it empty to turn it off.</p>
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

const extensionExample = JSON.stringify({ servers: [{ id: 'example', name: 'Example MCP', transport: 'stdio', command: 'your-mcp-server', args: [], enabled: false }], hooks: [{ id: 'example-hook', event: 'before_tool', tools: ['write_file', 'edit_file'], command: 'your-hook-command', timeout_seconds: 10, enabled: false }] }, null, 2)

function ExtensionsPanel({ session, onError }) {
  const query = scopeQuery(session)
  const { busy, error, run } = useRequest(onError)
  const [configuration, setConfiguration] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [skills, setSkills] = useState([])
  const [activeSkills, setActiveSkills] = useState(session?.active_skills || [])
  const [notice, setNotice] = useState('')
  const [tools, setTools] = useState(null)
  const [servers, setServers] = useState(null)
  useEffect(() => { void run(() => Promise.all([api('/extensions'), api(`/skills${query}`)]), ([config, skills]) => { setConfiguration(JSON.stringify(config, null, 2)); setSkills(skills); setLoaded(true) }) }, [query, run])
  return html`<section class="agent-tools-section" aria-label="Extensions">
    <h3>MCP servers and hooks</h3>
    <p>Configure stdio commands or HTTP server URLs and before_tool / after_tool / tool_failure hooks. Saving authorizes enabled servers to start.</p>
    <label class="agent-tools-config">Extension configuration (JSON)<textarea aria-label="Extension configuration (JSON)" disabled=${!loaded || busy} spellcheck=${false} value=${configuration} onInput=${e => setConfiguration(e.currentTarget.value)} /></label>
    <details class="agent-tools-example"><summary>Configuration example</summary><pre>${extensionExample}</pre><p>The optional tools list matches exact tool names. Omit it to match all tools; an empty list matches none. Hook input includes call_id; failure hooks also receive the original error.</p></details>
    <div class="agent-tools-actions"><button class="primary" disabled=${!loaded || busy || !configuration.trim()} onClick=${() => { setNotice(''); setTools(null); setServers(null); void run(() => api('/extensions', 'PUT', JSON.parse(configuration)), config => { setConfiguration(JSON.stringify(config, null, 2)); setNotice('Configuration saved.') }) }}>Save configuration</button>
      <button class="outline" disabled=${busy} onClick=${() => { setNotice(''); setTools(null); setServers(null); void run(() => api('/extensions/test', 'POST'), result => { setTools(result.tools); setServers(result.servers || null) }) }}>Test saved configuration</button></div>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}${notice ? html`<p role="status">${notice}</p>` : null}
    ${tools ? html`<div class="agent-tools-test" role="status">${tools.length ? html`<strong>Available tools</strong><ul>${tools.map(tool => html`<li key=${tool}>${tool}</li>`)}</ul>` : 'No tools reported by the saved configuration.'}</div>` : null}
    ${servers ? html`<div class="agent-tools-list" aria-label="MCP server test results"><p class="field-help">Results from the last test, not a live connection monitor.</p>${servers.map(server => html`<div class="agent-tools-row" key=${server.id}><div><strong>${server.name}</strong><small>${server.status === 'connected' ? `Connected during test · ${server.tools.length} tools` : server.status === 'disabled' ? 'Disabled · not started' : 'Failed'}</small>${server.error ? html`<p class="form-error">${server.error}</p>` : null}</div></div>`)}</div>` : null}
    <h3>Skills</h3>${!session ? html`<p class="field-help">Start a conversation to enable skills.</p>` : null}
    <div class="agent-tools-list">${skills.map(skill => html`<div class="agent-tools-row" key=${skill.id}><label><input type="checkbox" disabled=${busy || !session} checked=${activeSkills.includes(skill.id)} onChange=${e => { if (session) void run(() => api(`/sessions/${encodeURIComponent(session.id)}/skills`, 'POST', { skill_id: skill.id, enabled: e.currentTarget.checked }), result => setActiveSkills(result.active_skills)) }} />${skill.name}<small>${skill.description}</small><small>${skill.path}</small></label></div>`)}</div>
    ${!busy && !skills.length ? html`<p class="muted">No installed skills found.</p>` : null}
  </section>`
}

// MCP tool names the server accepts in a chat's turned-off list.
const MCP_TOOL = /^mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_.-]+$/

// Turned-off tools of this chat: GET /tools?session_id= lists the built-in and feature tools.
function ToolsPanel({ session, onError, onSaveTools }) {
  const { busy, error, run } = useRequest(onError)
  const [tools, setTools] = useState(null)
  const [off, setOff] = useState([])
  const [mcpName, setMcpName] = useState('')
  const [mcpError, setMcpError] = useState('')
  const [notice, setNotice] = useState('')
  const id = session?.id
  useEffect(() => {
    if (id) void run(() => api(`/tools?session_id=${encodeURIComponent(id)}`), result => { setTools(result.tools || []); setOff(result.disabled || []) })
  }, [id, run])
  if (!session) return html`<section class="agent-tools-section" aria-label="Tools"><p>Start a conversation to choose its tools.</p></section>`
  const names = new Set((tools || []).map(tool => tool.name))
  const extra = off.filter(name => !names.has(name))
  const toggle = (name, available) => { setNotice(''); setOff(current => available ? current.filter(item => item !== name) : [...current, name]) }
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
    <div class="agent-tools-list">${(tools || []).map(tool => html`<div class="agent-tools-row" key=${tool.name}><label class="tools-toggle">
      <input type="checkbox" disabled=${busy} checked=${!off.includes(tool.name)} onChange=${e => toggle(tool.name, e.currentTarget.checked)} />
      <span><strong>${tool.name}</strong>${tool.description ? html`<small>${tool.description}</small>` : null}</span></label></div>`)}</div>
    <form class="agent-tools-form" onSubmit=${e => { e.preventDefault(); addMcp() }}>
      <label>Turn off an MCP tool by name<input spellcheck=${false} disabled=${busy} value=${mcpName} placeholder="mcp__docs__search"
        onInput=${e => { setMcpName(e.currentTarget.value); setMcpError('') }} /></label>
      <button class="outline" disabled=${busy || !mcpName.trim()}>Add</button>
    </form>
    ${mcpError ? html`<p class="form-error" role="alert">${mcpError}</p>` : null}
    ${extra.length ? html`<ul class="tool-chips" aria-label="Turned-off MCP tools">${extra.map(name => html`<li key=${name}><span>${name}</span>
      <button class="icon-button" disabled=${busy} aria-label=${`Turn on ${name}`} onClick=${() => toggle(name, true)}><${X} size=${13} /></button></li>`)}</ul>` : null}
    <div class="agent-tools-actions"><button class="primary" disabled=${busy || tools === null} onClick=${save}>Save tools</button></div>
    ${notice ? html`<p role="status">${notice}</p>` : null}
  </section>`
}
