import { html, useAutoFocus, useEffect, useRef, useState } from '../lib.js'
import { ArrowLeftRight, Code2, Folder, Pencil, Plus, Settings as SettingsIcon, Trash2, X } from '../icons.js'

export function Brand({ small = false }) {
  return html`<span class=${`brand-symbol ${small ? 'small' : ''}`} aria-hidden="true"><i /><i /><i /><i /></span>`
}

function RenameForm({ session, title, setTitle, saving, error, onSave, onCancel }) {
  const input = useAutoFocus()
  return html`<form class="conversation-rename" aria-label=${`Rename ${session.title}`} aria-busy=${saving}
    onSubmit=${event => { event.preventDefault(); onSave() }}
    onKeyDown=${event => { if (event.key === 'Escape') { event.preventDefault(); if (!saving) onCancel() } }}>
    <label for=${`rename-${session.id}`}>Conversation name</label>
    <input ref=${input} id=${`rename-${session.id}`} required maxLength=${160} value=${title} disabled=${saving}
      aria-invalid=${!!error} aria-describedby=${error ? `rename-error-${session.id}` : undefined}
      onFocus=${event => event.currentTarget.select()} onInput=${event => setTitle(event.currentTarget.value)} />
    <div><button type="submit" class="primary" disabled=${saving || !title.trim()}>${saving ? 'Saving…' : 'Save name'}</button>
      <button type="button" class="outline" disabled=${saving} onClick=${onCancel}>Cancel rename</button></div>
    ${error ? html`<p id=${`rename-error-${session.id}`} class="form-error" role="alert">${error}</p>` : null}
  </form>`
}

function ConversationRow({ session, selected, onSelect, onDelete, onRename }) {
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const renameButton = useRef(null)
  const restoreFocus = useRef(false)
  useEffect(() => {
    if (!editing && restoreFocus.current) {
      renameButton.current?.focus()
      restoreFocus.current = false
    }
  }, [editing])
  const finish = () => { restoreFocus.current = true; setEditing(false) }
  const save = async () => {
    if (saving || !title.trim()) return
    setSaving(true); setError('')
    try { await onRename(session.id, title); finish() }
    catch (error) { setError(error instanceof Error ? error.message : 'Could not rename this conversation.') }
    finally { setSaving(false) }
  }
  return html`<div class=${`conversation-row ${selected ? 'selected' : ''}`}>
    ${editing ? html`<${RenameForm} session=${session} title=${title} saving=${saving} error=${error}
      setTitle=${value => { setTitle(value); setError('') }} onSave=${() => void save()} onCancel=${finish} />` : html`
      <button onClick=${onSelect} title=${`${session.title}\nSession: ${session.id}`} aria-current=${selected ? 'page' : undefined}>
        ${session.status !== 'idle' ? html`<span class="activity-dot" />` : null}<span>${session.title}<small class="session-id"> · ${session.id.slice(0, 8)}</small></span>
      </button>
      <button ref=${renameButton} class="rename-session" aria-label=${`Rename ${session.title}`} title="Rename conversation"
        onClick=${() => { setTitle(session.title); setError(''); setEditing(true) }}><${Pencil} size=${14} /></button>
      <button class="delete-session" aria-label=${`Delete ${session.title}`} onClick=${() => {
        if (confirm('Delete this conversation? Your project files will remain.')) onDelete()
      }}><${Trash2} size=${14} /></button>`}
  </div>`
}

// Props: settings, connection, sessions, activeId, open, close, onNew, onSelect, onSettings, onDelete, onRename
export default function Sidebar(props) {
  const folder = props.settings?.workspace.split('/').filter(Boolean).pop() || 'Choose a project'
  return html`
    ${props.open ? html`<button class="sidebar-scrim" aria-label="Close sidebar" onClick=${props.close} />` : null}
    <aside class=${`sidebar ${props.open ? 'visible' : ''}`}>
      <div class="brand"><${Brand} /><div><span>Local</span></div><span class="code-label"><${Code2} size=${17} />Code</span>
        <button class="icon-button mobile-only" onClick=${props.close} aria-label="Close sidebar"><${X} /></button>
      </div>
      <button class="new-conversation outline" aria-label="New conversation" onClick=${() => { props.onNew(); props.close() }}><${Plus} />New</button>
      <button class="project-button" onClick=${props.onSettings} title=${props.settings?.workspace}>
        <${Folder} /><span>${folder}</span><${ArrowLeftRight} size=${15} />
      </button>
      <div class="conversation-list">
        <p class="section-label">Conversations</p>
        ${props.sessions.length === 0 ? html`<p class="empty-sessions">Your conversations will appear here.</p>` :
          props.sessions.map(s => html`<${ConversationRow} key=${s.id} session=${s} selected=${props.activeId === s.id}
            onSelect=${() => { props.onSelect(s.id); props.close() }} onDelete=${() => props.onDelete(s.id)} onRename=${props.onRename} />`)}
      </div>
      <footer class="sidebar-footer">
        <button class="settings-button" onClick=${props.onSettings}><${SettingsIcon} />Settings</button>
        <div class="connection-state" title=${props.connection?.error || props.settings?.host || 'Checking connection'}>
          <span class=${`status-dot ${props.connection?.connected ? 'connected' : props.connection ? 'disconnected' : ''}`} />
          ${props.connection === null ? 'Checking Databricks…' : props.connection.connected ? 'Databricks connected' : 'Connection needs attention'}
        </div>
      </footer>
    </aside>`
}
