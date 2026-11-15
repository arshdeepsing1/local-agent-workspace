import { html, visibleParts, useEffect, useRef, useState } from '../lib.js'
import { api } from '../api.js'
import DialogShell from './DialogShell.js'

const count = n => `${n} subagent ${n === 1 ? 'conversation' : 'conversations'}`
const SHOWN_RUNNING = 5

// Asked once before deleting a chat that has subagent chats. The main button deletes the chat and its subagent
// chats; the other keeps them as chats of their own. A subagent that still runs is stopped by either (deleting its
// chat stops it), so the dialog names those and asks for an explicit confirmation first.
// Props: session, children (number), onDelete(children: 'delete'|'keep') -> Promise, onClose
export default function DeleteConversationDialog({ session, children, onDelete, onClose }) {
  const cancel = useRef(null)
  const [busy, setBusy] = useState(false)
  const [listed, setListed] = useState(null)  // the chat's subagent chats with their status, or null while unknown
  const [stop, setStop] = useState(false)
  useEffect(() => {
    let alive = true
    api(`/sessions/${encodeURIComponent(session.id)}/children`)
      .then(items => { if (alive && Array.isArray(items)) setListed(items) })
      .catch(() => {})  // Without the list the dialog still asks, and says nothing about running ones.
    return () => { alive = false }
  }, [session.id])
  const running = (listed || []).filter(child => child.status && child.status !== 'idle')
  const total = listed ? Math.max(listed.length, children) : children
  const unconfirmed = running.length > 0 && !stop
  const choose = async mode => {
    if (busy || unconfirmed) return
    setBusy(true)
    try { await onDelete(mode) } finally { setBusy(false); onClose() }
  }
  const label = child => child.subagent?.task || child.title || child.id
  // Cancel has focus first, a click outside decides nothing, and Escape does not close it while it deletes.
  return html`<${DialogShell} className="delete-conversation-dialog" label="Delete conversation" title="Delete conversation"
    onClose=${onClose} busy=${busy} backdrop=${false} initialFocus=${cancel}
    footer=${html`<button type="button" class="outline" ref=${cancel} disabled=${busy} onClick=${onClose}>Cancel</button>
        <button type="button" class="outline" data-destructive disabled=${busy || unconfirmed} onClick=${() => void choose('keep')}>Delete, keep subagents</button>
        <button type="button" class="primary" data-destructive disabled=${busy || unconfirmed} onClick=${() => void choose('delete')}>Delete with subagents</button>`}>
      <p class="logical-order">Delete “${visibleParts(session.title)}”? It has ${count(total)}. Your project files will remain.</p>
      ${listed ? html`<p role="status">${running.length ? `${running.length} of them ${running.length === 1 ? 'is' : 'are'} still running.` : 'None of them is running.'}</p>` : null}
      ${running.length ? html`<ul class="logical-order" aria-label="Running subagents">
        ${running.slice(0, SHOWN_RUNNING).map(child => html`<li key=${child.id}>${visibleParts(label(child))}</li>`)}
        ${running.length > SHOWN_RUNNING ? html`<li>and ${running.length - SHOWN_RUNNING} more</li>` : null}</ul>
        <label class="settings-checkbox"><input type="checkbox" checked=${stop} disabled=${busy}
          onChange=${event => setStop(event.currentTarget.checked)} />
          <span>Stop ${running.length === 1 ? 'it' : 'them'} first. Deleting this chat stops ${running.length === 1 ? 'it' : 'them'} either way.</span></label>` : null}
      <p class="field-help">Delete with subagents removes this chat and its subagent chats. Delete, keep subagents removes only this chat; the subagent chats stay as chats of their own.</p>
  <//>`
}
