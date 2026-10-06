// Attention: browser notifications, the "(n)" tab-title prefix, and which
// conversations changed since they were last opened. Notifications name only
// a status and the chat title, never transcript text.
import { useEffect, useRef, useState } from './lib.js'

const NOTIFY_KEY = 'local.notifications'
const SEEN_KEY = 'local.seenConversations'
const TITLE_LIMIT = 60
// Statuses of a turn in progress; a manual compaction ("compacting") is not a reply.
const TURN_STATUSES = new Set(['running', 'naming', 'delegating', 'awaiting_approval'])
export const ERROR_REASONS = new Set(['error', 'inference_error'])

// localStorage can be missing or throw (private windows, blocked storage).
function stored(key) {
  try { return window.localStorage.getItem(key) } catch { return null }
}
function store(key, value) {
  try { window.localStorage.setItem(key, value) } catch { /* Kept for this page only. */ }
}
const notificationsSupported = () => typeof window.Notification === 'function'

export function shortTitle(title) {
  const text = String(title || '').replace(/\s+/g, ' ').trim() || 'Untitled conversation'
  return text.length <= TITLE_LIMIT ? text : `${text.slice(0, TITLE_LIMIT - 1).trimEnd()}…`
}

// What a status change needs to tell you: { state, body } or null.
export function attentionFor(previous, session) {
  if (previous === session.status) return null
  const title = shortTitle(session.title)
  if (session.status === 'awaiting_approval') return { state: 'approval', body: `Needs your approval: ${title}` }
  // A subagent's end is reported by its parent's turn; a stop was asked for.
  if (session.status !== 'idle' || !TURN_STATUSES.has(previous) || session.is_subagent || session.terminal_reason === 'stopped') return null
  return ERROR_REASONS.has(session.terminal_reason)
    ? { state: 'error', body: `Stopped with an error: ${title}` }
    : { state: 'finished', body: `Finished: ${title}` }
}

// sessions: summaries from /api/sessions (kept current by polling and the selected chat's stream).
export function useAttention({ sessions, activeId, ready, onSelect }) {
  const [enabled, setEnabled] = useState(() => stored(NOTIFY_KEY) === 'on'
    && notificationsSupported() && window.Notification.permission === 'granted')
  const [permission, setPermission] = useState(() => notificationsSupported() ? window.Notification.permission : 'unsupported')
  // Conversation id -> its `updated` time when it was last open here (server time, so no clock skew).
  const [seen, setSeen] = useState(() => {
    try { const value = JSON.parse(stored(SEEN_KEY)); return value && typeof value === 'object' ? value : null }
    catch { return null }
  })
  const previous = useRef(null)
  const current = useRef({})
  current.current = { enabled, activeId, onSelect }
  const baseTitle = useRef(null)

  // First use: every existing chat counts as read, so old chats are not all unread.
  useEffect(() => {
    if (!ready || seen) return
    setSeen(Object.fromEntries(sessions.map(item => [item.id, item.updated])))
  }, [ready, seen, sessions])

  // The open chat is read; forget chats that no longer exist.
  useEffect(() => {
    if (!ready || !seen) return
    const known = new Set(sessions.map(item => item.id))
    const open = sessions.find(item => item.id === activeId)
    const stale = Object.keys(seen).some(id => !known.has(id))
    if (!stale && !(open && !(seen[open.id] >= open.updated))) return
    const next = Object.fromEntries(Object.entries(seen).filter(([id]) => known.has(id)))
    if (open) next[open.id] = Math.max(open.updated, seen[open.id] || 0)
    setSeen(next)
  }, [ready, seen, sessions, activeId])
  useEffect(() => { if (seen) store(SEEN_KEY, JSON.stringify(seen)) }, [seen])

  // "(n) " in the tab title while n conversations wait for approval.
  useEffect(() => {
    if (baseTitle.current === null) baseTitle.current = document.title.replace(/^\(\d+\) /, '')
    const waiting = sessions.filter(item => item.status === 'awaiting_approval').length
    document.title = waiting ? `(${waiting}) ${baseTitle.current}`.trim() : baseTitle.current
  }, [sessions])
  useEffect(() => () => { if (baseTitle.current !== null) document.title = baseTitle.current }, [])

  // One notification per status change, for a hidden page or a chat that is not open.
  useEffect(() => {
    if (!ready) return
    const before = previous.current
    previous.current = new Map(sessions.map(item => [item.id, item.status]))
    if (!before) return
    const { enabled, activeId, onSelect } = current.current
    if (!enabled || !notificationsSupported() || window.Notification.permission !== 'granted') return
    for (const item of sessions) {
      const note = attentionFor(before.get(item.id) ?? 'idle', item)
      if (!note || (!document.hidden && item.id === activeId)) continue
      try {
        const notification = new window.Notification('Local', { body: note.body, tag: `${item.id}:${note.state}` })
        notification.onclick = () => { window.focus(); onSelect(item.id); notification.close() }
      } catch { /* Some browsers allow notifications only from a service worker. */ }
    }
  }, [ready, sessions])

  const toggle = async () => {
    if (enabled) { setEnabled(false); store(NOTIFY_KEY, 'off'); return }
    if (!notificationsSupported()) return
    let answer = window.Notification.permission
    // Ask only when you turn notifications on, never on page load.
    if (answer === 'default') {
      try { answer = await window.Notification.requestPermission() } catch { answer = window.Notification.permission }
    }
    setPermission(answer)
    if (answer === 'granted') { setEnabled(true); store(NOTIFY_KEY, 'on') }
  }
  const unread = item => !!seen && item.id !== activeId && !(seen[item.id] >= item.updated)
  return {
    notifications: { enabled, permission, toggle },
    unread,
    // The last turn ended in an error and the chat has not been opened since.
    failed: item => item.status === 'idle' && ERROR_REASONS.has(item.terminal_reason) && unread(item),
    // A rename by you is not news: keep a read chat read.
    renamed: (id, before, after) => setSeen(current => current && before !== undefined && current[id] >= before
      ? { ...current, [id]: Math.max(after, current[id]) } : current),
  }
}
