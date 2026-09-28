import { html, useCallback, useEffect, useRef, useState } from '../lib.js'
import { X } from '../icons.js'
import { api } from '../api.js'
import ConversationTransfer from './ConversationTransfer.js'
import UsagePanel from './UsagePanel.js'

// Props: session, workspace?, onClose, onSelectSession, onError
const profileLabels = { inherit: 'Inherit permitted tools', read_only: 'Read-only files', file_editor: 'Read and edit files' }
const taskStatuses = ['pending', 'in_progress', 'completed', 'cancelled']
const scopeQuery = session => session ? `?session_id=${encodeURIComponent(session.id)}` : ''

function useRequest(onError) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const alive = useRef(false)
  const report = useRef(onError)
  report.current = onError
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const run = useCallback(async (operation, success) => {
    setBusy(true); setError('')
    try {
      const value = await operation()
      if (alive.current) success(value)
    } catch (e) {
      if (alive.current) { setError(e.message); report.current(e.message) }
    } finally { if (alive.current) setBusy(false) }
  }, [])
  return { busy, error, run }
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
      <nav class="agent-tools-tabs" aria-label="Agent tools sections">${['Recovery', 'Usage', 'Worktrees', 'Tasks', 'Extensions', 'Conversation'].map(name => html`<button key=${name} aria-pressed=${tab === name} onClick=${() => setTab(name)}>${name}</button>`)}</nav>
      ${tab === 'Recovery' ? html`<${Recovery} ...${props} />` : tab === 'Usage' ? html`<${UsagePanel} session=${props.session} onError=${props.onError} />` : tab === 'Worktrees' ? html`<${Worktrees} ...${props} />` : tab === 'Tasks' ? html`<${Tasks} ...${props} />` : tab === 'Extensions' ? html`<${ExtensionsPanel} ...${props} />` :
        html`<${ConversationTransfer} sessionId=${props.session?.id} workspace=${props.session?.workspace || props.workspace || ''} busy=${!!props.session && props.session.status !== 'idle'} onError=${props.onError} onSelectSession=${id => { props.onSelectSession(id); props.onClose() }} />`}
    </div>
  </dialog>`
}

function Recovery({ session, onError }) {
  const query = scopeQuery(session)
  const { busy, error, run } = useRequest(onError)
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
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
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
