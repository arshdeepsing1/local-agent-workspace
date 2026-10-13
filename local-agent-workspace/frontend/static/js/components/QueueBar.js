import { html, useState } from '../lib.js'
import { Pencil, X } from '../icons.js'

// Messages sent while the agent works (session.queued_messages, pushed as { type: 'queue' }).
// While busy they go to the agent after its current step; after a stop or failure they wait,
// paused, until sent with 'Send queued messages', edited or removed.
const clipped = text => text.length > 120 ? `${text.slice(0, 119)}…` : text

// Props: session, onRemove(id) -> Promise, onEdit(item) -> Promise, onSend() -> Promise, onError(message)
export default function QueueBar({ session, onRemove, onEdit, onSend, onError }) {
  const [working, setWorking] = useState(false)
  const queue = session?.queued_messages || []
  if (!queue.length && !session?.queue_error) return null
  const busy = session.status !== 'idle'
  const act = async action => {
    setWorking(true)
    try { await action() } catch (e) { onError(e.message) } finally { setWorking(false) }
  }
  return html`<div class="queue-bar" role="group" aria-label="Queued messages">
    ${session.queue_error ? html`<p class="queue-error" role="alert">${session.queue_error}</p>` : null}
    ${queue.length ? html`<div class="queue-heading">${busy ? 'Queued — sent after the agent’s current step'
      : 'Not sent yet: queued messages wait when a reply is stopped or fails.'}
      ${!busy ? html`<button class="outline" disabled=${working} onClick=${() => void act(onSend)}>Send queued messages</button>` : null}</div>` : null}
    ${queue.map(item => html`<div class="queue-item" key=${item.id}>
      <span class="queue-text" title=${item.text}>${clipped(item.text)}</span>
      ${item.paused ? html`<small class="queue-paused">Paused</small>` : null}
      <button class="icon-button" aria-label="Edit queued message" title="Edit queued message" disabled=${working} onClick=${() => void act(() => onEdit(item))}><${Pencil} size=${14} /></button>
      <button class="icon-button" aria-label="Remove queued message" title="Remove queued message" disabled=${working} onClick=${() => void act(() => onRemove(item.id))}><${X} size=${14} /></button>
    </div>`)}
  </div>`
}
