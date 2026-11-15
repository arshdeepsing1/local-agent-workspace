import { html, useRef, useState } from '../lib.js'
import { api } from '../api.js'
import DialogShell from './DialogShell.js'
import { omittedMessage } from '../commands.js'

// A branch name stamped with the local time: <prefix>/YYYY-MM-DD-HHmmss (to the second, so a second
// worktree made within the same minute gets its own default name).
export function stampedBranch(prefix, now = new Date()) {
  const pad = value => String(value).padStart(2, '0')
  return `${prefix}/${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
}

// Fork from here: a new chat with the conversation up to a reply (POST /sessions/<id>/fork with a body).
// Props: session, beforeEventId (the first user message after the reply; null for the last reply),
// onSelectSession(new id), onClose.
export default function ForkDialog({ session, beforeEventId, onSelectSession, onClose }) {
  const titleInput = useRef(null)
  const [title, setTitle] = useState(`${session.title || 'New conversation'} (fork)`.slice(0, 160))
  const [worktree, setWorktree] = useState(false)
  const [branch, setBranch] = useState(() => stampedBranch('fork'))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [made, setMade] = useState(null)  // { id, note }: the fork exists but left subagent chats out
  const submit = async e => {
    e.preventDefault()
    if (saving || made) return
    setSaving(true); setError('')
    const body = { ...(beforeEventId ? { before_event_id: beforeEventId } : {}),
      ...(title.trim() ? { title: title.trim() } : {}), ...(worktree ? { worktree_branch: branch.trim() } : {}) }
    try {
      const created = await api(`/sessions/${encodeURIComponent(session.id)}/fork`, 'POST', body)
      const note = omittedMessage(created.children_omitted, 'copied')
      if (note) { setMade({ id: created.id, note }); setSaving(false); return }  // Shown before the fork opens.
      onClose()
      onSelectSession(created.id)
    } catch (e) { setError(e.message); setSaving(false) }
  }
  // Closing after the fork was made still opens it: it exists either way.
  const leave = () => { onClose(); if (made) onSelectSession(made.id) }
  // The title is what to change first, so it has focus rather than Close.
  return html`<${DialogShell} className="fork-dialog" label="Fork conversation" title="Fork from here"
    description=${html`<p>A new chat with the conversation up to this reply, with the subagent chats it started up to here. It starts in Manual mode, without this chat's folder grants or rules.</p>`}
    onClose=${leave} onSubmit=${submit} initialFocus=${titleInput} busy=${saving}
    footer=${html`${made ? html`<button type="button" class="primary" onClick=${leave}>Open the fork</button>`
        : html`<button type="button" class="outline" disabled=${saving} onClick=${onClose}>Cancel</button>
        <button type="submit" class="primary" disabled=${saving || (worktree && !branch.trim())}>Fork</button>`}`}>
      <label>Title<input ref=${titleInput} maxLength=${160} spellcheck=${false} value=${title} disabled=${saving}
        onInput=${e => setTitle(e.currentTarget.value)} /></label>
      <label class="settings-checkbox"><input type="checkbox" checked=${worktree} disabled=${saving}
        onChange=${e => setWorktree(e.currentTarget.checked)} /><span>Create it in a new Git worktree</span></label>
      ${worktree ? html`<label>Branch name<input maxLength=${200} spellcheck=${false} value=${branch} disabled=${saving}
          onInput=${e => setBranch(e.currentTarget.value)} /></label>
        <p class="field-help">The worktree starts from the last commit; uncommitted changes are not copied.</p>` : null}
      ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
      ${made ? html`<p role="status">Fork created. ${made.note}</p>` : null}
  <//>`
}
