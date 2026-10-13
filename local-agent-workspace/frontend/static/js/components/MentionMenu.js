import { html, useEffect, useRef, useState } from '../lib.js'

// @ file mentions in the message box. Typing "@" at the start of a word lists the
// project's files (GET /mention-files?q=, 150 ms after the last key); choosing one
// inserts "@<path> ". When the message is sent, the server attaches a snapshot of
// each mentioned file that exists (backend/local_agent/mentions.py).

let menus = 0

// The same tokens as mentions.parse_mentions on the server: not inside code, not after a
// word character, '.', '@' or '/', and without trailing punctuation.
const MENTION = /(?<![\w.@/])@([^\s@`'"<>|]+)/g
const TRAILING = /[.,;:!?)\]}]+$/
const RANGE = /^(.+?)(?::(\d+)(?:-(\d+))?|#L(\d+)(?:-L?(\d+))?)$/
const masked = text => text.replace(/```[\s\S]*?(?:```|$)/g, match => ' '.repeat(match.length))
  .replace(/`[^`\n]*`/g, match => ' '.repeat(match.length))

// [{ token, path, start, end }] of a message, in order and without repeats.
export function parseMentions(text) {
  const found = []
  for (const match of masked(text || '').matchAll(MENTION)) {
    const token = match[1].replace(TRAILING, '')
    if (!token || found.some(item => item.token === token)) continue
    const range = RANGE.exec(token)
    const a = range ? Number(range[2] || range[4]) : 0
    const b = range ? Number(range[3] || range[5] || a) : 0
    found.push(range && a >= 1 && b >= a ? { token, path: range[1], start: a, end: b } : { token, path: token, start: null, end: null })
  }
  return found
}

export const mentionLabel = item => `@${item.path}${item.start ? `:${item.start === item.end ? item.start : `${item.start}-${item.end}`}` : ''}`

// The message without one token (every place it is a whole token). Only the spaces around each
// removed token change: one space stays between the words it separated, none at a line end.
export function removeMention(text, token) {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const pattern = new RegExp(`([ \\t]*)(?<![\\w.@/])@${escaped}(?=$|\\s|[.,;:!?)\\]}]+(?:\\s|$))([ \\t]*)`, 'g')
  return text.replace(pattern, (match, before, after, offset, whole) => {
    const next = whole[offset + match.length]
    return next === undefined || next === '\n' || next === '\r' ? '' : before
  })
}

// The "@word" being typed at the caret: { query, start } or null.
export function mentionAt(value, caret) {
  const before = value.slice(0, caret)
  const match = /(?:^|\s)@([^\s@`'"<>|]*)$/.exec(before)
  return match ? { query: match[1], start: before.length - match[1].length - 1 } : null
}

// Options: value, setValue, caret (selection start of the message box) and setCaret, input (its ref),
// loadFiles(query) -> Promise<[{ path, name }]>. Returns a key handler, input props and the menu.
export function useMentionMenu({ value, setValue, caret, setCaret, input, loadFiles }) {
  const [id] = useState(() => `mention-menu-${++menus}`)
  const [focused, setFocused] = useState(false)
  const [dismissed, setDismissed] = useState(null)
  const [result, setResult] = useState({ query: null, files: null, error: '' })
  const [highlight, setHighlight] = useState({ query: null, index: 0 })
  const load = useRef(loadFiles)
  load.current = loadFiles
  const menu = useRef(null)
  const at = loadFiles ? mentionAt(value, caret) : null
  const query = at?.query ?? null
  const open = at !== null && focused && value !== dismissed
  useEffect(() => { if (dismissed !== null && value !== dismissed) setDismissed(null) }, [value, dismissed])
  useEffect(() => {
    if (!open) return
    let current = true
    const timer = setTimeout(() => {
      load.current(query).then(files => { if (current) setResult({ query, files, error: '' }) },
        error => { if (current) setResult({ query, files: [], error: error.message }) })
    }, 150)
    return () => { current = false; clearTimeout(timer) }
  }, [open, query])
  const files = open && result.query === query ? result.files : null
  const matches = files || []
  const index = highlight.query === query ? Math.min(highlight.index, Math.max(matches.length - 1, 0)) : 0
  useEffect(() => { if (open) menu.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }) }, [open, index, query])
  const choose = file => {
    const rest = value.slice(caret).replace(/^\S*/, '').replace(/^\s/, '')
    const inserted = `${value.slice(0, at.start)}@${file.path} `
    setValue(inserted + rest)
    setCaret(inserted.length)
    requestAnimationFrame(() => { input.current?.focus(); input.current?.setSelectionRange(inserted.length, inserted.length) })
  }
  const keyDown = event => {
    if (!open || event.isComposing) return false
    if (event.key === 'Escape') { event.preventDefault(); setDismissed(value); return true }
    if (!matches.length) return false
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setHighlight({ query, index: (index + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length })
      return true
    }
    if ((event.key === 'Enter' || event.key === 'Tab') && !event.shiftKey) { event.preventDefault(); choose(matches[index]); return true }
    return false
  }
  const listed = open && matches.length > 0
  const inputProps = {
    'aria-controls': listed ? id : undefined, 'aria-activedescendant': listed ? `${id}-${index}` : undefined,
    onFocus: () => setFocused(true), onBlur: () => setFocused(false),
  }
  const note = result.query === query && result.error ? result.error : files === null ? 'Finding files…'
    : `No file matches @${query}.`
  const view = open ? html`<div class="slash-menu mention-menu" ref=${menu} onMouseDown=${e => e.preventDefault()}>
    <div class="slash-menu-heading">Files</div>
    ${listed ? html`<div role="listbox" id=${id} aria-label="Files">${matches.map((file, i) => html`<div key=${file.path} id=${`${id}-${i}`}
      role="option" aria-selected=${i === index} class="slash-option" onMouseMove=${() => { if (i !== index) setHighlight({ query, index: i }) }}
      onClick=${() => choose(file)}>
      <span class="slash-command">${file.name}<small>${file.path}</small></span>
    </div>`)}</div>` : html`<p class="slash-menu-note" role=${result.error && result.query === query ? 'alert' : 'status'}>${note}</p>`}
  </div>` : null
  return { open, listed, inputProps, keyDown, menu: view }
}
