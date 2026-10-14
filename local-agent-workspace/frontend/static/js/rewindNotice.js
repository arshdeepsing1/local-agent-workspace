import { html, useEffect, useState } from './lib.js'
import { X } from './icons.js'

// The notice after an in-place rewind (Edit and resend, Retry, Rewind), per conversation:
// Map<session id, { title (of the archived copy), backupId, draftKept }>. It is shown above the
// message box, not in the transcript: the transcript stays scrolled to its end after a rewind, and
// a rewind to the first message leaves no messages (App then shows the welcome screen).
const notices = new Map()
const subscribers = new Set()

export function setRewindNotice(sessionId, notice) {
  if (notice) notices.set(sessionId, notice)
  else notices.delete(sessionId)
  for (const notify of [...subscribers]) notify()
}

export function backupTitle(title) {
  return `${title || 'New conversation'} (before rewind)`.slice(0, 160)
}

function useRewindNotice(sessionId) {
  const [, setVersion] = useState(0)
  useEffect(() => {
    const notify = () => setVersion(version => version + 1)
    subscribers.add(notify)
    return () => { subscribers.delete(notify) }
  }, [])
  return sessionId ? notices.get(sessionId) || null : null
}

// Props: sessionId, onSelectSession(id) opens the archived earlier version.
export function RewindNotice({ sessionId, onSelectSession }) {
  const notice = useRewindNotice(sessionId)
  if (!notice) return null
  return html`<div class="rewind-notice" role="status">
    <span>Rewound. The earlier version is saved as the archived conversation “${notice.title}”.${notice.draftKept ? ' Your draft was kept.' : ''}</span>
    ${notice.backupId && onSelectSession ? html`<button class="outline" onClick=${() => onSelectSession(notice.backupId)}>Open earlier version</button>` : null}
    <button class="icon-button" aria-label="Dismiss rewind notice" onClick=${() => setRewindNotice(sessionId, null)}><${X} size=${14} /></button>
  </div>`
}
