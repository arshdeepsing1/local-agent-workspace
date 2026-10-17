import { html, useRef, useState } from '../lib.js'
import { Pencil, X } from '../icons.js'

// Messages sent while the agent works (session.queued_messages, pushed as { type: 'queue' }).
// While busy they go to the agent after its current step; after a stop or failure they wait,
// paused, until sent with 'Send queued messages', edited or removed.
const clipped = (text, size = 120) => text.length > size ? `${text.slice(0, size - 1)}…` : text
const messageBox = () => document.querySelector('textarea[aria-label="Message"]')

// Props: session, onRemove(id) -> Promise, onEdit(item) -> Promise, onSend() -> Promise, onError(message)
export default function QueueBar({ session, onRemove, onEdit, onSend, onError }) {
  const [working, setWorking] = useState(false)
  const bar = useRef(null)
  const queue = session?.queued_messages || []
  if (!queue.length && !session?.queue_error) return null
  const busy = session.status !== 'idle'
  // focus(): where keyboard focus goes once the action is done and the page has updated, since the
  // button that was used is disabled meanwhile and may be gone.
  const act = async (action, focus) => {
    setWorking(true)
    try { await action() } catch (e) { onError(e.message) } finally {
      setWorking(false)
      setTimeout(() => focus()?.focus())
    }
  }
  // After a removal: the Remove button now in that row (or the last one), else the message box.
  const nextRemove = index => () => {
    const buttons = [...(bar.current?.querySelectorAll('.queue-remove') || [])]
    return buttons[Math.min(index, buttons.length - 1)] || messageBox()
  }
  return html`<div class="queue-bar" role="group" aria-label="Queued messages" ref=${bar}>
    ${session.queue_error ? html`<p class="queue-error" role="alert">${session.queue_error}</p>` : null}
    ${queue.length ? html`<div class="queue-heading">${busy ? 'Queued — sent after the agent’s current step'
      : 'Not sent yet: queued messages wait when a reply is stopped or fails.'}
      ${!busy ? html`<button class="outline" disabled=${working} onClick=${() => void act(onSend, messageBox)}>Send queued messages</button>` : null}</div>` : null}
    ${queue.map((item, index) => {
      const name = clipped(item.text, 40)
      return html`<div class="queue-item" key=${item.id}>
      <span class="queue-text" title=${item.text}>${clipped(item.text)}</span>
      ${item.command?.name ? html`<small class="queue-tag" title=${item.command.path || ''}>Command /${item.command.name}</small>` : null}
      ${item.verbatim ? html`<small class="queue-tag">Plain text</small>` : null}
      ${(item.uploads || []).map(ref => html`<small class="queue-tag" key=${ref.id} title="Attached file">${ref.name || 'Attached file'}</small>`)}
      ${item.paused ? html`<small class="queue-paused">Paused</small>` : null}
      <button class="icon-button" aria-label=${`Edit queued message: ${name}`} title="Edit queued message" disabled=${working} onClick=${() => void act(() => onEdit(item), messageBox)}><${Pencil} size=${14} /></button>
      <button class="icon-button queue-remove" aria-label=${`Remove queued message: ${name}`} title="Remove queued message" disabled=${working} onClick=${() => void act(() => onRemove(item.id), nextRemove(index))}><${X} size=${14} /></button>
    </div>`
    })}
  </div>`
}
