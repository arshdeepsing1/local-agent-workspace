// Unsent message drafts, saved in this browser's localStorage so they survive a
// reload. One entry per conversation id, plus 'new' for the new-conversation
// view. Storage can be unavailable or full; drafts then stay in memory only.
import { resolve, useEffect, useMemo, useRef, useState } from './lib.js'

const PREFIX = 'local-workspace:draft:'
const INDEX = 'local-workspace:drafts' // { [key]: last saved, epoch ms }
export const MAX_DRAFT_BYTES = 100 * 1024
export const MAX_DRAFTS = 50
const SAVE_DELAY_MS = 500
const bytes = text => new TextEncoder().encode(text).length

function readIndex() {
  try {
    const index = JSON.parse(localStorage.getItem(INDEX) || '{}')
    return index && typeof index === 'object' && !Array.isArray(index) ? index : {}
  } catch { return {} }
}
function writeIndex(index) {
  try { localStorage.setItem(INDEX, JSON.stringify(index)) } catch { /* Storage unavailable. */ }
}

export function loadDraft(key) {
  try {
    const text = localStorage.getItem(PREFIX + key)
    return typeof text === 'string' ? text : null
  } catch { return null }
}

export function removeDraft(key) {
  try { localStorage.removeItem(PREFIX + key) } catch { return }
  const index = readIndex()
  if (key in index) { delete index[key]; writeIndex(index) }
}

// An empty or oversized draft removes the saved one: an older, shorter
// version restored after a reload would be misleading.
export function saveDraft(key, text) {
  if (!text || bytes(text) > MAX_DRAFT_BYTES) { removeDraft(key); return }
  const index = readIndex()
  index[key] = Date.now()
  // Keep the newest drafts; this one is the newest.
  const evicted = Object.keys(index).sort((a, b) => index[b] - index[a]).slice(MAX_DRAFTS)
  try {
    for (const old of evicted) { localStorage.removeItem(PREFIX + old); delete index[old] }
    localStorage.setItem(PREFIX + key, text)
  } catch {
    delete index[key]
    try { localStorage.removeItem(PREFIX + key) } catch { /* Storage unavailable. */ }
  }
  writeIndex(index)
}

// The composer draft of each view (viewKey), saved under storeKey: the
// conversation id, or 'new' before the conversation exists. When the new view
// becomes a conversation, its draft moves from 'new' to the id.
// Returns [draft, setDraft, forget(sentText, ...storeKeys)]; forget removes a
// sent draft at once. Text typed while the message was sending is saved again.
export function useSavedDrafts(viewKey, storeKey) {
  const [drafts, setDrafts] = useState({})
  const known = Object.prototype.hasOwnProperty.call(drafts, viewKey)
  const restored = useMemo(() => known ? null : loadDraft(storeKey), [known, viewKey, storeKey])
  const draft = known ? drafts[viewKey] : restored || ''
  const setDraft = value => setDrafts(current => ({
    ...current, [viewKey]: resolve(value, Object.prototype.hasOwnProperty.call(current, viewKey) ? current[viewKey] : restored || ''),
  }))
  // Keep a restored draft with its view, even after the view's id changes.
  useEffect(() => {
    if (!known && restored) setDrafts(current => Object.prototype.hasOwnProperty.call(current, viewKey) ? current : { ...current, [viewKey]: restored })
  }, [known, restored, viewKey])

  const pending = useRef(null)
  const timer = useRef(0)
  const latest = useRef(draft)
  latest.current = draft
  const [revision, setRevision] = useState(0)
  const flush = () => {
    clearTimeout(timer.current)
    const next = pending.current
    pending.current = null
    if (next) saveDraft(next.key, next.text)
  }
  const previous = useRef({ viewKey, storeKey })
  useEffect(() => {
    const before = previous.current
    previous.current = { viewKey, storeKey }
    if (before.viewKey === viewKey && before.storeKey === 'new' && storeKey !== 'new') removeDraft('new')
    if (!known) return
    pending.current = { key: storeKey, text: draft }
    clearTimeout(timer.current)
    timer.current = setTimeout(flush, SAVE_DELAY_MS)
  }, [draft, known, viewKey, storeKey, revision])
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
    if (latest.current && latest.current.trim() !== sent) setRevision(value => value + 1)
  }
  return [draft, setDraft, forget]
}
