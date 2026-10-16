import { html, useCallback, useEffect, useRef, useState } from '../lib.js'
import { api } from '../api.js'

// Agent tools → Memory: the notes of this chat's project (backend memory.py), kept in the app's state folder.
// The agent adds notes with remember (each one approved); here the user reads, adds, edits and deletes them.
// Props: session (null: the default project), onError
const scope = session => session ? `?session_id=${encodeURIComponent(session.id)}` : ''
const count = (value, one, many = `${one}s`) => `${value.toLocaleString('en-US')} ${value === 1 ? one : many}`
const short = text => text.length > 40 ? `${text.slice(0, 39)}…` : text

export default function MemoryPanel({ session, onError }) {
  const query = scope(session)
  const [memory, setMemory] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState('')
  const [editing, setEditing] = useState(null)  // { id, text } while a note is edited
  const alive = useRef(false)
  const report = useRef(onError)
  report.current = onError
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const run = useCallback(async (operation, done) => {
    setBusy(true); setError(''); setNotice('')
    try {
      const value = await operation()
      if (alive.current) { setMemory(value); done?.(value) }
    } catch (e) {
      if (alive.current) { setError(e.message); report.current(e.message) }
    } finally { if (alive.current) setBusy(false) }
  }, [])
  useEffect(() => { void run(() => api(`/memory${query}`)) }, [query, run])
  const notes = memory?.notes || []
  const limit = memory?.max_note_chars || 500
  const add = event => {
    event.preventDefault()
    if (busy || !draft.trim()) return
    void run(() => api(`/memory${query}`, 'POST', { text: draft }), () => { setDraft(''); setNotice('Note added.') })
  }
  const save = event => {
    event.preventDefault()
    if (busy || !editing?.text.trim()) return
    void run(() => api(`/memory/${encodeURIComponent(editing.id)}${query}`, 'PATCH', { text: editing.text }),
      () => { setEditing(null); setNotice('Note saved.') })
  }
  const remove = note => void run(() => api(`/memory/${encodeURIComponent(note.id)}${query}`, 'DELETE'),
    () => { if (editing?.id === note.id) setEditing(null); setNotice('Note deleted.') })
  const clear = () => {
    if (!window.confirm(`Delete all ${count(notes.length, 'note')} of this project's memory? This cannot be undone.`)) return
    void run(() => api(`/memory${query}`, 'DELETE'), () => { setEditing(null); setNotice('Memory cleared.') })
  }
  return html`<section class="agent-tools-section memory-panel" aria-label="Memory">
    <div class="agent-tools-section-heading"><h3>Project memory</h3>
      <button class="outline" disabled=${busy} onClick=${() => void run(() => api(`/memory${query}`))}>Refresh memory</button></div>
    <p>Short notes about this project that the agent saved with your approval, or that you added. Every later chat in this project, its worktrees included, sees them in its system message as data; they never override your instructions or permissions.</p>
    ${memory?.project ? html`<p class="field-help memory-project">Project: <code>${memory.project}</code></p>` : null}
    ${memory && !memory.enabled ? html`<p class="settings-note" role="status">Project memory is off in Settings → Agent: the agent cannot save notes, and chats do not see these.</p>` : null}
    ${memory?.problem ? html`<p class="form-error" role="alert">${memory.problem}</p>` : null}
    ${memory ? html`<p class="field-help">${count(notes.length, 'note')} of ${memory.max_notes} · ${memory.bytes.toLocaleString('en-US')} of ${memory.max_bytes.toLocaleString('en-US')} bytes</p>` : null}
    ${notes.length ? html`<ul class="memory-notes" aria-label="Memory notes">${notes.map(note => html`<li class="memory-note" key=${note.id}>
      ${editing?.id === note.id ? html`<form class="agent-tools-form" onSubmit=${save}>
        <label>Edit note<textarea rows=${3} maxLength=${limit} spellcheck=${true} value=${editing.text} readOnly=${busy}
          onInput=${e => { const text = e.currentTarget.value; setEditing(current => ({ ...current, text })) }} /></label>
        <div class="agent-tools-actions"><button type="button" class="outline" disabled=${busy} onClick=${() => setEditing(null)}>Cancel</button>
          <button class="primary" disabled=${busy || !editing.text.trim()}>Save note</button></div>
      </form>` : html`<div class="memory-note-text"><span>${note.text}</span><small>${note.date}</small></div>
        <div class="agent-tools-actions">
          <button class="outline" disabled=${busy} aria-label=${`Edit note: ${short(note.text)}`} onClick=${() => setEditing({ id: note.id, text: note.text })}>Edit</button>
          <button class="outline" disabled=${busy} aria-label=${`Delete note: ${short(note.text)}`} onClick=${() => remove(note)}>Delete</button>
        </div>`}
    </li>`)}</ul>` : memory ? html`<p class="muted">No notes yet. Ask the agent to remember something about this project, or add a note below.</p>` : null}
    <form class="agent-tools-form" onSubmit=${add}>
      <label>New note<textarea rows=${2} maxLength=${limit} value=${draft} readOnly=${busy} placeholder="Tests run with pytest -q from the backend folder"
        onInput=${e => setDraft(e.currentTarget.value)} /></label>
      <div class="agent-tools-actions">${notes.length ? html`<button type="button" class="outline" disabled=${busy} onClick=${clear}>Clear memory</button>` : null}
        <button class="primary" disabled=${!memory || busy || !draft.trim()}>Add note</button></div>
    </form>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}${notice ? html`<p role="status">${notice}</p>` : null}
    <p class="field-help">Kept in this app's state folder for the repository, never in the repository itself, and not included in conversation exports. Notes are one line of up to ${limit} characters and never hold secrets.</p>
  </section>`
}
