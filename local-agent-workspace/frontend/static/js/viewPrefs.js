// Settings → Appearance → Group tool calls: two or more finished read-only tool cards in a row fold under one line
// ("Ran 3 commands"). On by default; kept in this browser's localStorage ('off' when turned off), like Text size.
import { useEffect, useState } from './lib.js'

const KEY = 'group-tool-calls'
const CHANGED = 'local-view-prefs'  // dispatched on window when this page changes a preference
let chosen = null  // this page's choice, for when storage is blocked (private windows)

export function groupToolCalls() {
  try {
    const value = window.localStorage.getItem(KEY)
    if (value !== null) return value !== 'off'
  } catch { /* Storage blocked: this page's choice or the default. */ }
  return chosen ?? true
}

export function setGroupToolCalls(value) {
  chosen = !!value
  try {
    if (chosen) window.localStorage.removeItem(KEY)
    else window.localStorage.setItem(KEY, 'off')
  } catch { /* Kept for this page only. */ }
  window.dispatchEvent(new Event(CHANGED))
}

// { group }: read again when Settings changes it, or another tab of the app does.
export function useViewPrefs() {
  const [group, setGroup] = useState(groupToolCalls)
  useEffect(() => {
    const update = () => setGroup(groupToolCalls())
    window.addEventListener(CHANGED, update)
    window.addEventListener('storage', update)
    return () => {
      window.removeEventListener(CHANGED, update)
      window.removeEventListener('storage', update)
    }
  }, [])
  return { group }
}
