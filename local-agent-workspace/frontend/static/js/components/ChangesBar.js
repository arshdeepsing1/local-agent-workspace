import { html, useState } from '../lib.js'
import { Check, ChevronDown, ChevronRight, File, Undo2 } from '../icons.js'

export const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`
export const STATUS_LABELS = { created: 'New file', modified: 'Modified', deleted: 'Deleted', unavailable: 'Unavailable' }

export function DiffStat({ added, removed }) {
  return html`<span class="diff-stat"><span class="added" aria-label=${`${plural(added, 'line')} added`}>+${added}</span>
    <span class="removed" aria-label=${`${plural(removed, 'line')} removed`}>−${removed}</span></span>`
}

// The files this conversation's agent changed and nobody has kept or undone yet,
// above the message box (like "N files changed" in an editor's chat view).
// changes: { files: [{ path, status, added, removed, current_hash, error? }], added, removed, error? } | null
// Props: changes, busy (a response is running; Undo waits for it), onOpen(path),
//   onAction(action, files) -> Promise, onError(message)
export default function ChangesBar({ changes, busy, onOpen, onAction, onError }) {
  const [open, setOpen] = useState(false)
  const [working, setWorking] = useState(false)
  const files = changes?.files || []
  if (!files.length && !changes?.error) return null
  const run = async (action, items) => {
    if (!items.length) return
    setWorking(true)
    try { await onAction(action, items.map(item => ({ path: item.path, current_hash: item.current_hash }))) }
    catch (e) { onError(e.message) }
    finally { setWorking(false) }
  }
  // An unreadable file cannot be undone, only kept (no longer tracked).
  const undoable = files.filter(item => item.current_hash)
  const undoTitle = busy ? 'Stop the response before undoing changes.' : 'Restore these files to how they were before the agent changed them'
  return html`<section class="changes-bar" aria-label="Changed files">
    ${files.length ? html`<div class="changes-summary">
      <button class="changes-toggle" aria-expanded=${open} onClick=${() => setOpen(value => !value)}>
        ${open ? html`<${ChevronDown} size=${15} />` : html`<${ChevronRight} size=${15} />`}
        <span>${plural(files.length, 'file')} changed</span><${DiffStat} added=${changes.added} removed=${changes.removed} />
      </button>
      <div class="changes-actions">
        <button class="primary" aria-label="Keep all changes" title="Accept the changes to every file" disabled=${working} onClick=${() => void run('keep', files)}>Keep</button>
        <button class="outline" aria-label="Undo all changes" title=${undoTitle} disabled=${working || busy || !undoable.length} onClick=${() => void run('undo', undoable)}>Undo</button>
      </div>
    </div>` : null}
    ${open && files.length ? html`<ul class="changes-list">${files.map(item => {
      const slash = item.path.lastIndexOf('/')
      return html`<li key=${item.path} class="changes-file">
        <button class="changes-open" title=${`Review ${item.path}`} onClick=${() => onOpen(item.path)}>
          <${File} size=${14} /><span class="changes-name">${item.path.slice(slash + 1)}</span>
          ${slash > 0 ? html`<span class="changes-dir">${item.path.slice(0, slash)}</span>` : null}
          ${item.status !== 'modified' ? html`<small class=${`changes-status ${item.status}`}>${STATUS_LABELS[item.status] || item.status}</small>` : null}
        </button>
        ${item.error ? html`<span class="changes-unavailable" title=${item.error}>${item.error}</span>` : html`<${DiffStat} added=${item.added} removed=${item.removed} />`}
        <button class="icon-button" aria-label=${`Keep changes to ${item.path}`} title="Keep" disabled=${working} onClick=${() => void run('keep', [item])}><${Check} size=${15} /></button>
        <button class="icon-button" aria-label=${`Undo changes to ${item.path}`} title=${busy ? undoTitle : 'Undo'} disabled=${working || busy || !item.current_hash} onClick=${() => void run('undo', [item])}><${Undo2} size=${15} /></button>
      </li>`
    })}</ul>` : null}
    ${changes.error ? html`<p class="changes-error" role="alert">Could not check changed files: ${changes.error}</p>` : null}
  </section>`
}
