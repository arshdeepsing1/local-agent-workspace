import { html, useAutoFocus, useState } from '../lib.js'
import { api } from '../api.js'

// Files a refused file rewind names (a 409 with detail.files).
export function RefusedFiles({ error }) {
  const files = Array.isArray(error?.detail?.files) ? error.detail.files : []
  return files.length ? html`<ul class="rewind-refused" aria-label="Files that changed">${files.map(path => html`<li key=${path}>${path}</li>`)}</ul>` : null
}

// Edit and resend: replaces a user message and everything after it (POST /rewind with resend_text).
// Props: sessionId, event (the user event), onSent(result), onCancel.
export default function EditMessage({ sessionId, event, onSent, onCancel }) {
  const [text, setText] = useState(event.text || '')
  const [code, setCode] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)
  const input = useAutoFocus()
  const send = async () => {
    if (sending || !text.trim()) return
    setSending(true); setError(null)
    try {
      const result = await api(`/sessions/${encodeURIComponent(sessionId)}/rewind`, 'POST',
        { before_event_id: event.id, conversation: true, code, resend_text: text })
      onSent(result)
    } catch (e) { setError(e); setSending(false) }
  }
  const onKeyDown = e => {
    if (e.key === 'Escape') { e.preventDefault(); onCancel() }
    else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void send() }
  }
  return html`<div class="edit-message" onKeyDown=${onKeyDown}>
    <textarea ref=${input} aria-label="Edit message" rows=${Math.min(12, Math.max(3, text.split('\n').length + 1))}
      spellcheck=${false} maxLength=${50000} value=${text} disabled=${sending} onInput=${e => setText(e.currentTarget.value)} />
    <label class="edit-message-option"><input type="checkbox" checked=${code} disabled=${sending}
      onChange=${e => setCode(e.currentTarget.checked)} /><span>Also undo file changes made from this message on</span></label>
    <p class="field-help">Replaces this message and everything after it. The current version is kept as an archived conversation.</p>
    ${error ? html`<div class="form-error" role="alert"><p>${error.message}</p><${RefusedFiles} error=${error} /></div>` : null}
    <div class="edit-message-actions"><button type="button" class="outline" disabled=${sending} onClick=${onCancel}>Cancel edit</button>
      <button type="button" class="primary" disabled=${sending || !text.trim()} onClick=${() => void send()}>Send edited message</button></div>
  </div>`
}
