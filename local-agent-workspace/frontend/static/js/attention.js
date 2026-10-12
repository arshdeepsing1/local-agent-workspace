// Attention: browser notifications, the "(n)" tab-title prefix, and which
// conversations changed since they were last opened. Notifications name only
// a status and the chat title, never transcript text.
import { useEffect, useRef, useState } from './lib.js'
import { streamProtocols } from './api.js'

const NOTIFY_KEY = 'local.notifications'
const SEEN_KEY = 'local.seenConversations'
const TITLE_LIMIT = 60
// "interrupted": the server stopped during the turn.
export const ERROR_REASONS = new Set(['error', 'inference_error', 'interrupted'])

// localStorage can be missing or throw (private windows, blocked storage).
function stored(key) {
  try { return window.localStorage.getItem(key) } catch { return null }
}
function store(key, value) {
  try { window.localStorage.setItem(key, value) } catch { /* Kept for this page only. */ }
}
function parseSeen(text) {
  try { const value = JSON.parse(text); return value && typeof value === 'object' && !Array.isArray(value) ? value : null }
  catch { return null }
}
// Per chat, the later "last opened" time of two maps (other tabs write the same key);
// `forgotten` chats were deleted.
function mergeSeen(base, other, forgotten) {
  const next = { ...base }
  for (const [id, time] of Object.entries(other || {})) if (typeof time === 'number' && !(next[id] >= time)) next[id] = time
  for (const id of forgotten) delete next[id]
  return next
}
const sameSeen = (a, b) => Object.keys(a).length === Object.keys(b).length && Object.entries(a).every(([id, time]) => b[id] === time)
const notificationsSupported = () => typeof window.Notification === 'function'

export function shortTitle(title) {
  const text = String(title || '').replace(/\s+/g, ' ').trim() || 'Untitled conversation'
  return text.length <= TITLE_LIMIT ? text : `${text.slice(0, TITLE_LIMIT - 1).trimEnd()}…`
}

// What a change tells you: { state, body } or null. `before` is the chat's last
// known { status, turn_ended }. turn_ended changes when a turn ends, including one
// that failed during an automatic compaction or ended between two polls; the end
// of a manual compaction leaves it unchanged.
export function attentionFor(before, session) {
  const title = shortTitle(session.title)
  if (session.status === 'awaiting_approval') {
    return before.status === 'awaiting_approval' ? null : { state: 'approval', body: `Needs your approval: ${title}` }
  }
  // A turn that delegate_task started is reported by its parent's turn (a subagent chat
  // you continued yourself is not); a stop was asked for.
  if (session.status !== 'idle' || !session.turn_ended || session.turn_ended === before.turn_ended
    || session.turn_delegated || session.terminal_reason === 'stopped') return null
  if (ERROR_REASONS.has(session.terminal_reason)) return { state: 'error', body: `Stopped with an error: ${title}` }
  // Not finished: the chat offers Continue.
  if (session.terminal_reason === 'step_limit') return { state: 'step_limit', body: `Reached the step limit: ${title}` }
  return { state: 'finished', body: `Finished: ${title}` }
}

// Held open while this page is: the server's desktop notifications are only for
// when no page is open, with or without a chat selected.
function usePresence(ready) {
  useEffect(() => {
    if (!ready) return
    let socket = null, timer = null, closed = false
    const connect = () => {
      try { socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/presence`, streamProtocols()) }
      catch { return }
      socket.onclose = () => { if (!closed) timer = setTimeout(connect, 5000) }
    }
    connect()
    return () => { closed = true; clearTimeout(timer); socket?.close() }
  }, [ready])
}

// sessions: summaries from /api/sessions (kept current by polling and the selected chat's stream).
export function useAttention({ sessions, activeId, ready, onSelect }) {
  const [enabled, setEnabled] = useState(() => stored(NOTIFY_KEY) === 'on'
    && notificationsSupported() && window.Notification.permission === 'granted')
  const [permission, setPermission] = useState(() => notificationsSupported() ? window.Notification.permission : 'unsupported')
  // Conversation id -> its `updated` time when it was last open here (server time, so no clock skew).
  const [seen, setSeen] = useState(() => parseSeen(stored(SEEN_KEY)))
  const forgotten = useRef(new Set())
  const known = useRef(null)
  const previous = useRef(null)
  // Conversation id -> the notification shown for it, closed when it no longer applies.
  const shown = useRef(new Map())
  const current = useRef({})
  current.current = { enabled, activeId, onSelect }
  const baseTitle = useRef(null)
  usePresence(ready)

  // First use: every existing chat counts as read, so old chats are not all unread.
  useEffect(() => {
    if (!ready || seen) return
    setSeen(Object.fromEntries(sessions.map(item => [item.id, item.updated])))
  }, [ready, seen, sessions])

  // The open chat is read; forget deleted chats (unknown ones on load, then those that leave the list).
  useEffect(() => {
    if (!ready || !seen) return
    const ids = new Set(sessions.map(item => item.id))
    const gone = known.current ? [...known.current].filter(id => !ids.has(id)) : Object.keys(seen).filter(id => !ids.has(id))
    known.current = ids
    gone.forEach(id => forgotten.current.add(id))
    const open = sessions.find(item => item.id === activeId)
    if (!gone.some(id => id in seen) && !(open && !(seen[open.id] >= open.updated))) return
    const next = mergeSeen(seen, {}, forgotten.current)
    if (open) next[open.id] = Math.max(open.updated, seen[open.id] || 0)
    setSeen(next)
  }, [ready, seen, sessions, activeId])
  // Merge with what other tabs stored, so one tab never undoes another's reads.
  useEffect(() => {
    if (seen) store(SEEN_KEY, JSON.stringify(mergeSeen(parseSeen(stored(SEEN_KEY)) || {}, seen, forgotten.current)))
  }, [seen])
  useEffect(() => {
    const onStorage = event => {
      if (event.key !== SEEN_KEY) return
      const other = parseSeen(event.newValue)
      if (!other) return
      setSeen(value => {
        const next = mergeSeen(value || {}, other, forgotten.current)
        return value && sameSeen(next, value) ? value : next
      })
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  // "(n) " in the tab title while n conversations wait for approval.
  useEffect(() => {
    if (baseTitle.current === null) baseTitle.current = document.title.replace(/^\(\d+\) /, '')
    const waiting = sessions.filter(item => item.status === 'awaiting_approval').length
    document.title = waiting ? `(${waiting}) ${baseTitle.current}`.trim() : baseTitle.current
  }, [sessions])
  useEffect(() => () => { if (baseTitle.current !== null) document.title = baseTitle.current }, [])

  const dismiss = id => {
    const notification = shown.current.get(id)
    shown.current.delete(id)
    try { notification?.close() } catch { /* Already gone. */ }
  }
  // Opening a chat answers its notification.
  useEffect(() => { if (activeId) dismiss(activeId) }, [activeId])

  // One notification per change, for a hidden page or a chat that is not open.
  useEffect(() => {
    if (!ready) return
    const before = previous.current
    previous.current = new Map(sessions.map(item => [item.id, { status: item.status, turn_ended: item.turn_ended }]))
    if (!before) return
    for (const id of shown.current.keys()) if (!previous.current.has(id)) dismiss(id)
    const { enabled, activeId, onSelect } = current.current
    for (const item of sessions) {
      // A chat seen for the first time (created or imported elsewhere) has no finished turn to report.
      const last = before.get(item.id) || { status: 'idle', turn_ended: item.turn_ended }
      if (last.status === item.status && last.turn_ended === item.turn_ended) continue
      // The previous notification no longer applies (an approval was answered, a new turn began).
      dismiss(item.id)
      const note = attentionFor(last, item)
      if (!note || !enabled || !notificationsSupported() || window.Notification.permission !== 'granted') continue
      if (!document.hidden && item.id === activeId) continue
      try {
        const notification = new window.Notification('Local', { body: note.body, tag: `${item.id}:${note.state}` })
        notification.onclick = () => { window.focus(); onSelect(item.id); notification.close() }
        shown.current.set(item.id, notification)
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
