import { html, useEffect, useRef, useState } from '../lib.js'

const count = n => `${n} subagent ${n === 1 ? 'conversation' : 'conversations'}`

// Asked before deleting a chat that has subagent chats.
// Props: session, children (number), onDelete(children: 'delete'|'keep') -> Promise, onClose
export default function DeleteConversationDialog({ session, children, onDelete, onClose }) {
  const dialog = useRef(null)
  const cancel = useRef(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const node = dialog.current
    node?.showModal()
    cancel.current?.focus()
    return () => { if (node?.open) node.close() }
  }, [])
  const choose = async mode => {
    if (busy) return
    setBusy(true)
    try { await onDelete(mode) } finally { setBusy(false); onClose() }
  }
  return html`<dialog ref=${dialog} class="settings-dialog delete-conversation-dialog" aria-label="Delete conversation"
    onCancel=${event => { event.preventDefault(); if (!busy) onClose() }}>
    <div class="dialog-content">
      <p>Delete “${session.title}”? It has ${count(children)}. Your project files will remain.</p>
      <footer>
        <button type="button" class="outline" ref=${cancel} disabled=${busy} onClick=${onClose}>Cancel</button>
        <button type="button" class="outline" disabled=${busy} onClick=${() => void choose('keep')}>Delete, keep subagents</button>
        <button type="button" class="primary" disabled=${busy} onClick=${() => void choose('delete')}>Delete with subagents</button>
      </footer>
    </div>
  </dialog>`
}
