import { html } from '../lib.js'
import { blockingChat } from '../api.js'

// The chat an error names as blocking an action ({ message, session_id, title }), or null.
// Takes an ApiError (its 409 detail) or only its message.
export function blockerOf(error) {
  if (!error) return null
  if (typeof error === 'string') return blockingChat(error)
  return typeof error.detail?.session_id === 'string' ? error.detail : blockingChat(error.message || '')
}

// "Open “<title>”" next to an error that names another chat. Nothing for the selected chat or a plain error.
export function OpenBlockingChat({ blocker, currentId, onSelect }) {
  if (!blocker || !onSelect || blocker.session_id === currentId) return null
  return html`<button class="outline open-blocking-chat" onClick=${() => onSelect(blocker.session_id)}>Open “${blocker.title}”</button>`
}
