import { html, useEffect, useState } from '../lib.js'
import { X } from '../icons.js'
import { api } from '../api.js'

// Agent tools → Extensions: MCP servers and hooks as a form, or as the JSON it saves ("Edit as JSON"),
// and an import of another tool's MCP servers (a preview first; imported servers are added disabled).
// Props: config (the saved configuration, null while loading), busy (a save or test runs), session,
// onSave(config) (the panel PUTs it), onError(message), actions (more buttons beside Save).
export const HOOK_EVENTS = ['before_tool', 'after_tool', 'tool_failure', 'user_prompt', 'session_start', 'agent_stop',
  'subagent_start', 'subagent_stop']
const TOOL_EVENTS = new Set(['before_tool', 'after_tool', 'tool_failure'])
let rows = 0
const rowKey = () => `row-${++rows}`
const lines = text => text === '' ? [] : text.split('\n')
const isObject = value => value && typeof value === 'object' && !Array.isArray(value)
// Arguments the form edits one per line: a list of strings without line breaks. Others stay in rest.
const lineArgs = args => Array.isArray(args) && args.every(arg => typeof arg === 'string' && !arg.includes('\n'))
// The credential-file keys a server's env or headers refer to.
const referencedKeys = server => Object.values({ ...(isObject(server.env) ? server.env : {}), ...(isObject(server.headers) ? server.headers : {}) })
  .filter(isObject).map(value => value.secret)

// The form's model of a configuration. rest keeps fields the form has no control for, so a round trip
// through the form never drops what the JSON said (the server still validates it).
export function toForm(config) {
  const { servers = [], hooks = [], ...rest } = isObject(config) ? config : {}
  return {
    rest,
    servers: (Array.isArray(servers) ? servers : []).map(server => {
      const { id = '', name = '', transport = 'stdio', command = '', args = [], url = '', env, headers,
        workspace_credentials: workspace, enabled = false, ...other } = isObject(server) ? server : {}
      const field = transport === 'http' ? 'headers' : 'env'
      const source = field === 'env' ? env : headers
      if (field === 'env' && headers !== undefined) other.headers = headers
      if (field === 'headers' && env !== undefined) other.env = env
      if (field === 'env' && workspace !== undefined) other.workspace_credentials = workspace
      // An argument with a line break (python -c with a script) would become several lines: kept as it is.
      const argsKept = !lineArgs(args)
      if (argsKept) other.args = args
      return { key: rowKey(), id: String(id), name: String(name), transport, command: String(command),
        args: argsKept ? '' : args.join('\n'), argsKept, url: String(url), enabled: enabled === true,
        workspace: field === 'headers' && workspace === true, rest: other,
        values: Object.entries(isObject(source) ? source : {}).map(([key, value]) => isObject(value)
          ? { key: rowKey(), name: key, secret: true, value: String(value.secret ?? ''), prefix: String(value.prefix ?? '') }
          : { key: rowKey(), name: key, secret: false, value: String(value ?? ''), prefix: '' }) }
    }),
    hooks: (Array.isArray(hooks) ? hooks : []).map(hook => {
      const { id = '', event = 'before_tool', command = '', timeout_seconds: timeout = 10, enabled = false, tools, ...other } =
        isObject(hook) ? hook : {}
      return { key: rowKey(), id: String(id), event, command: String(command), timeout: String(timeout), enabled: enabled === true,
        match: Array.isArray(tools) ? 'listed' : 'all', tools: Array.isArray(tools) ? tools.join('\n') : '', rest: other }
    }),
  }
}

export function toConfig(form) {
  return {
    ...form.rest,
    servers: form.servers.map(server => {
      const stdio = server.transport !== 'http'
      const values = Object.fromEntries(server.values.filter(item => item.name.trim() || item.value.trim()).map(item =>
        [item.name.trim(), item.secret ? { secret: item.value.trim(), ...(!stdio && item.prefix ? { prefix: item.prefix } : {}) } : item.value]))
      return { ...server.rest, id: server.id.trim(), name: server.name.trim() || server.id.trim(), transport: server.transport,
        ...(stdio ? { command: server.command, ...(server.argsKept ? {} : { args: lines(server.args) }) } : { url: server.url.trim() }),
        ...(Object.keys(values).length ? { [stdio ? 'env' : 'headers']: values } : {}),
        ...(!stdio && server.workspace ? { workspace_credentials: true } : {}),
        enabled: server.enabled }
    }),
    hooks: form.hooks.map(hook => ({ ...hook.rest, id: hook.id.trim(), event: hook.event, command: hook.command,
      timeout_seconds: Number(hook.timeout), enabled: hook.enabled,
      ...(hook.match === 'listed' ? { tools: hook.tools.split('\n').map(name => name.trim()).filter(Boolean) } : {}) })),
  }
}

const emptyServer = () => ({ key: rowKey(), id: '', name: '', transport: 'stdio', command: '', args: '', argsKept: false, url: '', enabled: false,
  workspace: false, values: [], rest: {} })
const emptyHook = () => ({ key: rowKey(), id: '', event: 'before_tool', command: '', timeout: '10', enabled: false, match: 'all',
  tools: '', rest: {} })
// An imported entry's ID, unique among the form's servers.
function freeId(id, taken) {
  let candidate = id
  for (let number = 2; taken.has(candidate); number++) candidate = `${id.slice(0, 20 - String(number).length - 1)}-${number}`
  taken.add(candidate)
  return candidate
}

function ServerFields({ server, index, busy, onChange, onRemove }) {
  const n = index + 1
  const stdio = server.transport !== 'http'
  const kind = stdio ? 'environment variable' : 'header'
  const patchValue = (row, changes) => onChange({ values: server.values.map((item, at) => at === row ? { ...item, ...changes } : item) })
  return html`<fieldset class="extension-entry" disabled=${busy}>
    <legend>Server ${n}${server.id ? `: ${server.id}` : ''}</legend>
    <div class="extension-fields">
      <label>ID<input aria-label=${`Server ${n} ID`} spellcheck=${false} maxLength=${20} value=${server.id} placeholder="docs"
        onInput=${e => onChange({ id: e.currentTarget.value })} /></label>
      <label>Name<input aria-label=${`Server ${n} name`} maxLength=${80} value=${server.name} placeholder="Docs search"
        onInput=${e => onChange({ name: e.currentTarget.value })} /></label>
      <label>Transport<select aria-label=${`Server ${n} transport`} value=${server.transport}
        onChange=${e => onChange({ transport: e.currentTarget.value })}>
        <option value="stdio">stdio (a local command)</option><option value="http">Streamable HTTP (a URL)</option></select></label>
    </div>
    ${stdio ? html`<label>Command<input aria-label=${`Server ${n} command`} spellcheck=${false} value=${server.command}
        placeholder="/absolute/path/to/server" onInput=${e => onChange({ command: e.currentTarget.value })} /></label>
      ${server.argsKept ? html`<p class="field-help" role="note" aria-label=${`Server ${n} arguments`}>Arguments: <code>${JSON.stringify(server.rest.args)}</code>.
          One of them has a line break, so the form keeps them as they are: change them with Edit as JSON.</p>`
        : html`<label>Arguments, one per line<textarea aria-label=${`Server ${n} arguments`} rows=${2} spellcheck=${false} value=${server.args}
        onInput=${e => onChange({ args: e.currentTarget.value })} /></label>`}`
      : html`<label>URL<input aria-label=${`Server ${n} URL`} spellcheck=${false} value=${server.url} placeholder="https://mcp.example.com/mcp"
        onInput=${e => onChange({ url: e.currentTarget.value })} /></label>
      <label class="extension-check"><input type="checkbox" aria-label=${`Server ${n} uses workspace credentials`} checked=${server.workspace}
        onChange=${e => onChange({ workspace: e.currentTarget.checked })} />Use workspace credentials</label>
      <p class="field-help">Sends your Databricks token as the Authorization header, only to an https URL on your workspace's own host and port (Databricks managed MCP servers under /api/2.0/mcp/).</p>`}
    <div class="extension-values" role="group" aria-label=${`Server ${n} ${stdio ? 'environment variables' : 'headers'}`}>
      <span class="extension-values-title">${stdio ? 'Environment variables' : 'Headers'}</span>
      ${server.values.map((item, row) => {
        const label = `Server ${n} ${kind} ${row + 1}`
        return html`<div class="extension-value-row" key=${item.key}>
          <input aria-label=${`${label} name`} spellcheck=${false} value=${item.name} placeholder=${stdio ? 'NAME' : 'Header-Name'}
            onInput=${e => patchValue(row, { name: e.currentTarget.value })} />
          <select aria-label=${`${label} kind`} value=${item.secret ? 'secret' : 'text'}
            onChange=${e => patchValue(row, { secret: e.currentTarget.value === 'secret' })}>
            <option value="secret">Secret reference</option><option value="text">Text</option></select>
          <input aria-label=${`${label} ${item.secret ? 'credential-file key' : 'value'}`} spellcheck=${false} value=${item.value}
            placeholder=${item.secret ? 'KEY in your credential file' : 'Value'} onInput=${e => patchValue(row, { value: e.currentTarget.value })} />
          ${!stdio && item.secret ? html`<input aria-label=${`${label} prefix`} spellcheck=${false} value=${item.prefix} placeholder="Prefix, e.g. Bearer"
            onInput=${e => patchValue(row, { prefix: e.currentTarget.value })} />` : null}
          <button type="button" class="icon-button" aria-label=${`Remove ${label}`}
            onClick=${() => onChange({ values: server.values.filter((_, at) => at !== row) })}><${X} size=${14} /></button>
        </div>`
      })}
      <button type="button" class="outline" onClick=${() => onChange({ values: [...server.values,
        { key: rowKey(), name: '', secret: true, value: '', prefix: '' }] })}>Add ${kind}</button>
    </div>
    <div class="extension-entry-actions">
      <label class="extension-check"><input type="checkbox" aria-label=${`Server ${n} enabled`} checked=${server.enabled}
        onChange=${e => onChange({ enabled: e.currentTarget.checked })} />Enabled</label>
      <button type="button" class="outline" aria-label=${`Remove server ${n}`} onClick=${onRemove}>Remove</button>
    </div>
  </fieldset>`
}

function HookFields({ hook, index, busy, onChange, onRemove }) {
  const n = index + 1
  return html`<fieldset class="extension-entry" disabled=${busy}>
    <legend>Hook ${n}${hook.id ? `: ${hook.id}` : ''}</legend>
    <div class="extension-fields">
      <label>ID<input aria-label=${`Hook ${n} ID`} spellcheck=${false} maxLength=${20} value=${hook.id} placeholder="check"
        onInput=${e => onChange({ id: e.currentTarget.value })} /></label>
      <label>Event<select aria-label=${`Hook ${n} event`} value=${hook.event} onChange=${e => onChange({ event: e.currentTarget.value })}>
        ${HOOK_EVENTS.map(event => html`<option value=${event} key=${event}>${event}</option>`)}</select></label>
      <label>Time limit (seconds)<input aria-label=${`Hook ${n} time limit`} type="number" min="1" max="30" step="1" value=${hook.timeout}
        onInput=${e => onChange({ timeout: e.currentTarget.value })} /></label>
    </div>
    <label>Command<input aria-label=${`Hook ${n} command`} spellcheck=${false} value=${hook.command} placeholder="your-hook-command"
      onInput=${e => onChange({ command: e.currentTarget.value })} /></label>
    ${TOOL_EVENTS.has(hook.event) || hook.match === 'listed' ? html`<label>Tools<select aria-label=${`Hook ${n} tools`} value=${hook.match}
        onChange=${e => onChange({ match: e.currentTarget.value })}>
        <option value="all">All tools</option><option value="listed">Only the tools listed</option></select></label>
      ${hook.match === 'listed' ? html`<label>Exact tool names, one per line (none listed: no tool)<textarea aria-label=${`Hook ${n} tool names`}
        rows=${2} spellcheck=${false} value=${hook.tools} onInput=${e => onChange({ tools: e.currentTarget.value })} /></label>` : null}` : null}
    <div class="extension-entry-actions">
      <label class="extension-check"><input type="checkbox" aria-label=${`Hook ${n} enabled`} checked=${hook.enabled}
        onChange=${e => onChange({ enabled: e.currentTarget.checked })} />Enabled</label>
      <button type="button" class="outline" aria-label=${`Remove hook ${n}`} onClick=${onRemove}>Remove</button>
    </div>
  </fieldset>`
}

function ImportPreview({ preview, onAdd, onCancel }) {
  const count = preview.entries.length
  // Keys the credential file already has (names only): an imported server gets them once enabled.
  const present = new Set(preview.present || [])
  const missing = preview.needs.filter(name => !present.has(name))
  const receiving = preview.entries.map(entry => ({ entry, keys: referencedKeys(entry).filter(name => present.has(name)) }))
    .filter(item => item.keys.length)
  const codes = names => names.map((name, index) => html`${index ? ', ' : ''}<code key=${name}>${name}</code>`)
  return html`<div class="extensions-preview" role="region" aria-label="Import preview">
    <h4>${count ? `${count} server${count === 1 ? '' : 's'} to add, disabled` : 'No server can be imported'}${preview.path ? html` <small>from ${preview.path}</small>` : null}</h4>
    ${count ? html`<ul>${preview.entries.map(entry => html`<li key=${entry.id}><strong>${entry.name}</strong> <code>${entry.id}</code>
      <small>${entry.transport === 'stdio' ? [entry.command, ...(entry.args || [])].join(' ') : entry.url}</small></li>`)}</ul>` : null}
    ${missing.length ? html`<p>Add these keys to your credential file before you enable them (the values were not imported): ${codes(missing)}.</p>` : null}
    ${receiving.length ? html`<p>Already in your credential file, and sent once you enable the server:</p><ul aria-label="Keys from your credential file">
      ${receiving.map(({ entry, keys }) => html`<li key=${entry.id}><strong>${entry.name}</strong> gets ${codes(keys)} (<code>${
        entry.transport === 'stdio' ? entry.command : entry.url}</code>)</li>`)}</ul>` : null}
    ${(preview.notes || []).length ? html`<p>Check before you enable:</p><ul>${preview.notes.map((item, index) => html`<li key=${index}><strong>${item.name}</strong>: ${item.text}</li>`)}</ul>` : null}
    ${preview.skipped.length ? html`<p>Not imported:</p><ul>${preview.skipped.map(item => html`<li key=${item.name}><strong>${item.name}</strong>: ${item.reason}</li>`)}</ul>` : null}
    ${(preview.ignored || []).length ? html`<p class="field-help">Left out (this app does not use them): ${preview.ignored.map(item => `${item.name}: ${item.fields.join(', ')}`).join('; ')}.</p>` : null}
    ${preview.source === 'project' ? html`<p class="import-warning" role="note">This .mcp.json is part of the project folder, so whoever wrote the project chose these servers. Enable one only if you trust its command and URL, above all one that would get a key from your credential file.</p>` : null}
    <p class="field-help">An imported server runs its command (for example npx or uvx) with your account's access once you enable it. Check each command and URL first.</p>
    <div class="agent-tools-actions"><button type="button" class="primary" disabled=${!count} onClick=${onAdd}>Add to the form</button>
      <button type="button" class="outline" onClick=${onCancel}>Cancel</button></div>
  </div>`
}

export default function ExtensionsEditor({ config, busy, session, onSave, onError, actions = null }) {
  const [mode, setMode] = useState('form')
  const [form, setForm] = useState(() => toForm(config || {}))
  const [text, setText] = useState(config ? JSON.stringify(config, null, 2) : '')
  const [problem, setProblem] = useState('')
  const [notice, setNotice] = useState('')
  const [pasted, setPasted] = useState('')
  const [preview, setPreview] = useState(null)
  const [importing, setImporting] = useState(false)
  useEffect(() => {
    if (!config) return
    setForm(toForm(config)); setText(JSON.stringify(config, null, 2)); setProblem('')
  }, [config])
  const loaded = !!config
  const locked = !loaded || busy
  // The JSON as an object, or null (the problem is shown).
  const parsed = () => {
    try { return JSON.parse(text) } catch (e) { setProblem(`The JSON is not valid: ${e.message}`); return null }
  }
  const switchTo = next => {
    setProblem(''); setNotice('')
    if (next === mode) return
    if (next === 'json') { setText(JSON.stringify(toConfig(form), null, 2)); setMode('json'); return }
    const value = parsed()
    if (value !== null) { setForm(toForm(value)); setMode('form') }
  }
  const save = () => {
    setProblem(''); setNotice('')
    const value = mode === 'json' ? parsed() : toConfig(form)
    if (value !== null) onSave(value)
  }
  const patch = (key, index, changes) => setForm(current => ({ ...current, [key]: current[key].map((item, at) => at === index ? { ...item, ...changes } : item) }))
  const remove = (key, index) => setForm(current => ({ ...current, [key]: current[key].filter((_, at) => at !== index) }))
  const load = async source => {
    setProblem(''); setNotice(''); setPreview(null); setImporting(true)
    try {
      const query = source === 'project' && session ? `?session_id=${encodeURIComponent(session.id)}` : ''
      setPreview(await api(`/extensions/import${query}`, 'POST', { source, ...(source === 'text' ? { text: pasted } : {}) }))
    } catch (e) { setProblem(e.message); onError?.(e.message) } finally { setImporting(false) }
  }
  const add = () => {
    let base = form
    if (mode === 'json') {
      const value = parsed()
      if (value === null) return
      base = toForm(value)
    }
    const taken = new Set(base.servers.map(server => server.id.trim()))
    const added = toForm({ servers: preview.entries.map(entry => ({ ...entry, id: freeId(entry.id, taken), enabled: false })) }).servers
    setForm({ ...base, servers: [...base.servers, ...added] }); setMode('form'); setPreview(null)
    setNotice(`Added ${added.length} disabled server${added.length === 1 ? '' : 's'} to the form. Save the configuration to keep ${added.length === 1 ? 'it' : 'them'}; enable a server to let chats start it.`)
  }
  return html`<div class="extensions-editor">
    <div class="extensions-mode" role="group" aria-label="Editor">
      <button type="button" class="outline" aria-pressed=${mode === 'form'} onClick=${() => switchTo('form')}>Form</button>
      <button type="button" class="outline" aria-pressed=${mode === 'json'} onClick=${() => switchTo('json')}>Edit as JSON</button>
    </div>
    ${mode === 'json' ? html`<label class="agent-tools-config">Extension configuration (JSON)<textarea aria-label="Extension configuration (JSON)"
        disabled=${locked} spellcheck=${false} value=${text} onInput=${e => setText(e.currentTarget.value)} /></label>`
      : html`<div class="extensions-form" aria-label="Extension configuration">
        <h4>MCP servers</h4>
        <p class="field-help">Up to 12 servers, 4 of them enabled. An environment variable or header is text or a secret reference: the name of a key in your credential file. Its value is read each time the server starts, never saved here, and hidden ([REDACTED]) in everything the app shows.</p>
        ${form.servers.map((server, index) => html`<${ServerFields} key=${server.key} server=${server} index=${index} busy=${locked}
          onChange=${changes => patch('servers', index, changes)} onRemove=${() => remove('servers', index)} />`)}
        <button type="button" class="outline" disabled=${locked} onClick=${() => setForm(current => ({ ...current, servers: [...current.servers, emptyServer()] }))}>Add server</button>
        <h4>Hooks</h4>
        ${form.hooks.map((hook, index) => html`<${HookFields} key=${hook.key} hook=${hook} index=${index} busy=${locked}
          onChange=${changes => patch('hooks', index, changes)} onRemove=${() => remove('hooks', index)} />`)}
        <button type="button" class="outline" disabled=${locked} onClick=${() => setForm(current => ({ ...current, hooks: [...current.hooks, emptyHook()] }))}>Add hook</button>
      </div>`}
    <details class="agent-tools-example extensions-import"><summary>Import MCP servers</summary>
      <p class="field-help">Reads another tool's MCP configuration and adds its servers to this form, disabled. Nothing is saved or started until you save and enable them. Environment and header values are never copied: each becomes a secret reference to add to your credential file. SSE servers are not supported.</p>
      <div class="agent-tools-actions">
        <button type="button" class="outline" disabled=${locked || importing || !session} onClick=${() => load('project')}>This project's .mcp.json</button>
        <button type="button" class="outline" disabled=${locked || importing} onClick=${() => load('desktop')}>Claude desktop app</button>
      </div>
      <label class="agent-tools-config extensions-paste">Or paste a configuration with mcpServers (JSON)<textarea aria-label="Configuration to import (JSON)"
        disabled=${locked || importing} spellcheck=${false} value=${pasted} onInput=${e => setPasted(e.currentTarget.value)} /></label>
      <div class="agent-tools-actions"><button type="button" class="outline" disabled=${locked || importing || !pasted.trim()} onClick=${() => load('text')}>Preview pasted servers</button></div>
    </details>
    ${preview ? html`<${ImportPreview} preview=${preview} onAdd=${add} onCancel=${() => setPreview(null)} />` : null}
    ${problem ? html`<p class="form-error" role="alert">${problem}</p>` : null}${notice ? html`<p role="status">${notice}</p>` : null}
    <div class="agent-tools-actions"><button type="button" class="primary" disabled=${locked || (mode === 'json' && !text.trim())} onClick=${save}>Save configuration</button>${actions}</div>
  </div>`
}
