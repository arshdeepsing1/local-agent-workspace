// Unsent message drafts, saved in this browser's localStorage so they survive a
// reload. One entry per conversation id, plus 'new' for the new-conversation
// view. A draft to send as plain text (the composer's Send as plain text) is
// saved with that mark. Storage can be unavailable or full; drafts then stay in
// memory only.
import { resolve, useEffect, useMemo, useRef, useState } from './lib.js'

const PREFIX = 'local-workspace:draft:'
const PLAIN = 'local-workspace:draft-plain:' // '1' beside a draft to send as plain text
const INDEX = 'local-workspace:drafts' // { [key]: last saved, epoch ms }
export const MAX_DRAFT_BYTES = 100 * 1024
export const MAX_DRAFTS = 50
const SAVE_DELAY_MS = 500
const EMPTY = { text: '', plain: false }
const bytes = text => new TextEncoder().encode(text).length
const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key)

function readIndex() {
  try {
    const index = JSON.parse(localStorage.getItem(INDEX) || '{}')
    return index && typeof index === 'object' && !Array.isArray(index) ? index : {}
  } catch { return {} }
}
function writeIndex(index) {
  try { localStorage.setItem(INDEX, JSON.stringify(index)) } catch { /* Storage unavailable. */ }
}

// The saved draft: { text, plain }, or null.
export function loadDraft(key) {
  try {
    const text = localStorage.getItem(PREFIX + key)
    return typeof text === 'string' ? { text, plain: localStorage.getItem(PLAIN + key) === '1' } : null
  } catch { return null }
}

function removeItems(key) {
  localStorage.removeItem(PREFIX + key)
  localStorage.removeItem(PLAIN + key)
}

export function removeDraft(key) {
  try { removeItems(key) } catch { return }
  const index = readIndex()
  if (key in index) { delete index[key]; writeIndex(index) }
}

// An empty or oversized draft removes the saved one: an older, shorter
// version restored after a reload would be misleading.
export function saveDraft(key, text, plain = false) {
  if (!text || bytes(text) > MAX_DRAFT_BYTES) { removeDraft(key); return }
  const index = readIndex()
  index[key] = Date.now()
  // Keep the newest drafts; this one is the newest.
  const evicted = Object.keys(index).sort((a, b) => index[b] - index[a]).slice(MAX_DRAFTS)
  try {
    for (const old of evicted) { removeItems(old); delete index[old] }
    localStorage.setItem(PREFIX + key, text)
    if (plain) localStorage.setItem(PLAIN + key, '1')
    else localStorage.removeItem(PLAIN + key)
  } catch {
    delete index[key]
    try { removeItems(key) } catch { /* Storage unavailable. */ }
  }
  writeIndex(index)
}

// The composer draft of each view (viewKey), saved under storeKey: the
// conversation id, or 'new' before the conversation exists. When the new view
// becomes a conversation, its draft moves from 'new' to the id. Send as plain
// text (plain) is kept and saved with the draft, so a draft restored in another
// view (every new-conversation view restores 'new') is still sent as typed.
// Returns { draft, setDraft, plain, setPlain, forget(sentText, ...storeKeys),
// release(storeKey) }; forget removes a sent draft at once. Text typed while the
// message was sending is saved again.
export function useSavedDrafts(viewKey, storeKey) {
  const [drafts, setDrafts] = useState({}) // viewKey -> { text, plain }
  const known = has(drafts, viewKey)
  const restored = useMemo(() => known ? null : loadDraft(storeKey), [known, viewKey, storeKey])
  const { text: draft, plain } = known ? drafts[viewKey] : restored || EMPTY
  const change = (field, value) => setDrafts(current => {
    const before = has(current, viewKey) ? current[viewKey] : restored || EMPTY
    return { ...current, [viewKey]: { ...before, [field]: resolve(value, before[field]) } }
  })
  const setDraft = value => change('text', value)
  const setPlain = value => change('plain', current => !!resolve(value, current))
  // Keep a restored draft with its view, even after the view's id changes.
  useEffect(() => {
    if (!known && restored) setDrafts(current => has(current, viewKey) ? current : { ...current, [viewKey]: restored })
  }, [known, restored, viewKey])

  const pending = useRef(null)
  const timer = useRef(0)
  const latest = useRef({ text: draft, plain })
  latest.current = { text: draft, plain }
  const [revision, setRevision] = useState(0)
  const flush = () => {
    clearTimeout(timer.current)
    const next = pending.current
    pending.current = null
    if (next) saveDraft(next.key, next.text, next.plain)
  }
  const previous = useRef({ viewKey, storeKey })
  useEffect(() => {
    const before = previous.current
    previous.current = { viewKey, storeKey }
    if (before.viewKey === viewKey && before.storeKey === 'new' && storeKey !== 'new') removeDraft('new')
    if (!known) return
    pending.current = { key: storeKey, text: draft, plain }
    clearTimeout(timer.current)
    timer.current = setTimeout(flush, SAVE_DELAY_MS)
  }, [draft, plain, known, viewKey, storeKey, revision])
  // Save at once when switching views and when the page is closed or reloaded.
  useEffect(() => flush, [viewKey, storeKey])
  useEffect(() => {
    window.addEventListener('pagehide', flush)
    return () => window.removeEventListener('pagehide', flush)
  }, [])
  const forget = (sent, ...keys) => {
    clearTimeout(timer.current)
    pending.current = null
    for (const key of new Set(keys)) if (key) removeDraft(key)
    // The composer keeps text typed during the send; schedule its save again.
    if (latest.current.text && latest.current.text.trim() !== sent) setRevision(value => value + 1)
  }
  // Sending the first message of a new conversation: its text leaves the shared
  // 'new' draft at once, so a new conversation opened during the send starts
  // empty. Returns restore(), which brings it back if the send fails: into the
  // new-conversation view now open when its box is empty, else saved as the draft.
  // Its Send as plain text comes back with it.
  const release = key => {
    const view = viewKey
    const sending = latest.current
    clearTimeout(timer.current)
    pending.current = null
    removeDraft(key)
    return () => {
      const open = previous.current
      if (open.viewKey === view) setRevision(value => value + 1)
      else if (open.storeKey === key) setDrafts(current => current[open.viewKey]?.text ? current : { ...current, [open.viewKey]: sending })
      else if (loadDraft(key) === null) saveDraft(key, sending.text, sending.plain)
    }
  }
  return { draft, setDraft, plain, setPlain, forget, release }
}
