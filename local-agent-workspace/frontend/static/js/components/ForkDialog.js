import { html, useEffect, useRef, useState } from '../lib.js'
import { X } from '../icons.js'
import { api } from '../api.js'

// A branch name stamped with the local time: <prefix>/YYYY-MM-DD-HHmm.
export function stampedBranch(prefix, now = new Date()) {
  const pad = value => String(value).padStart(2, '0')
  return `${prefix}/${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`
}

// Fork from here: a new chat with the conversation up to a reply (POST /sessions/<id>/fork with a body).
// Props: session, beforeEventId (the first user message after the reply; null for the last reply),
// onSelectSession(new id), onClose.
export default function ForkDialog({ session, beforeEventId, onSelectSession, onClose }) {
  const dialog = useRef(null)
  const titleInput = useRef(null)
  const [title, setTitle] = useState(`${session.title || 'New conversation'} (fork)`.slice(0, 160))
  const [worktree, setWorktree] = useState(false)
  const [branch, setBranch] = useState(() => stampedBranch('fork'))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    const node = dialog.current
    node?.showModal()
    // After showModal, which focuses the first control (Close); the title is what to change first.
    titleInput.current?.focus()
    return () => { if (node?.open) node.close() }
  }, [])
  const submit = async e => {
    e.preventDefault()
    if (saving) return
    setSaving(true); setError('')
    const body = { ...(beforeEventId ? { before_event_id: beforeEventId } : {}),
      ...(title.trim() ? { title: title.trim() } : {}), ...(worktree ? { worktree_branch: branch.trim() } : {}) }
    try {
      const created = await api(`/sessions/${encodeURIComponent(session.id)}/fork`, 'POST', body)
      onClose()
      onSelectSession(created.id)
    } catch (e) { setError(e.message); setSaving(false) }
  }
  return html`<dialog ref=${dialog} class="settings-dialog fork-dialog" aria-label="Fork conversation"
    onCancel=${onClose} onClick=${e => { if (e.target === dialog.current) onClose() }}>
    <form class="dialog-content" onSubmit=${submit}>
      <header><div><h2>Fork from here</h2><p>A new chat with the conversation up to this reply. It starts in Manual mode, without this chat's folder grants or rules.</p></div>
        <button type="button" class="icon-button" aria-label="Close" onClick=${onClose}><${X} size=${20} /></button></header>
      <label>Title<input ref=${titleInput} maxLength=${160} spellcheck=${false} value=${title} disabled=${saving}
        onInput=${e => setTitle(e.currentTarget.value)} /></label>
      <label class="settings-checkbox"><input type="checkbox" checked=${worktree} disabled=${saving}
        onChange=${e => setWorktree(e.currentTarget.checked)} /><span>Create it in a new Git worktree</span></label>
      ${worktree ? html`<label>Branch name<input maxLength=${200} spellcheck=${false} value=${branch} disabled=${saving}
          onInput=${e => setBranch(e.currentTarget.value)} /></label>
        <p class="field-help">The worktree starts from the last commit; uncommitted changes are not copied.</p>` : null}
      ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
      <footer><button type="button" class="outline" onClick=${onClose}>Cancel</button>
        <button type="submit" class="primary" disabled=${saving || (worktree && !branch.trim())}>Fork</button></footer>
    </form>
  </dialog>`
}
