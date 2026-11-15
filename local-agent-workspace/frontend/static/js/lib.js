// One import point for the UI runtime. Preact has React's component and hook
// model; htm gives JSX-like templates without a compiler:
//   html`<button class="primary" onClick=${save}>Save</button>`
// Differences from React worth remembering when editing components:
// - use `class`, and `onInput` for live text input (`onChange` fires on blur);
// - booleans such as `disabled=${busy}` and `aria-*` values work as in React;
// - SVG attributes keep their SVG spelling, for example `text-anchor`.
import { h, render } from 'preact'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'preact/hooks'
import htm from 'htm'

export const html = htm.bind(h)
export { render, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState }

// Apply a React-style state update (a value or an updater function).
export const resolve = (value, current) => typeof value === 'function' ? value(current) : value

// Focus an element once when it mounts (React's autoFocus).
export function useAutoFocus() {
  const ref = useRef(null)
  useEffect(() => { ref.current?.focus() }, [])
  return ref
}

// Text an approver reads before allowing an action must show what will run. Characters that Chromium draws as nothing,
// as blank space or in the wrong order are shown as visible escapes (\xNN below U+0100, \uNNNN above, \u{...} past
// U+FFFF; the same format as scripts/agent_run.py). The rule is built from Unicode categories so it stays complete:
//   escaped:  Cc (controls, but not newline and tab), Cf (format: bidi marks, zero-width, soft hyphen, tags ...),
//             Zl, Zp, Zs (every space but U+0020), Cs (a lone surrogate), Co (private use: no agreed look), Cn
//             (unassigned), Default_Ignorable_Code_Point, U+FFFC (the object replacement character) and a list of
//             characters in other categories that draw blank or nothing (variation selectors, combining grapheme
//             joiner, Hangul and Braille fillers, Khmer and Mongolian invisibles, the Unicode tag block);
//             also the third and later combining mark on one base (overlays stacked on "-" hide it);
//   kept:     text that needs a joiner: a variation selector or ZWJ inside an emoji sequence, and ZWNJ/ZWJ between
//             two letters of Arabic and the Indic scripts.
// Right-to-left words are not escaped but isolated: each run is its own part (a <bdi class="rtl-run"> when shown), so
// the bidi algorithm cannot reorder the ASCII around it.
const SHOWN_AS_IS = new Set([0x20, 0x0a, 0x09])
const NEEDS_ESCAPE_SOURCE = '[\\p{Cc}\\p{Cf}\\p{Zl}\\p{Zp}\\p{Zs}\\p{Cs}\\p{Co}\\p{Cn}\\p{Default_Ignorable_Code_Point}\\ufffc'
  + '\\u00ad\\u034f\\u0600-\\u0605\\u06dd\\u070f\\u0890\\u0891\\u08e2\\u115f\\u1160\\u17b4\\u17b5\\u180b-\\u180f\\u2800\\u3164'
  + '\\ufe00-\\ufe0f\\uffa0\\ufff9-\\ufffb\\u{110bd}\\u{110cd}\\u{13430}-\\u{1343f}\\u{1bca0}-\\u{1bca3}\\u{1d173}-\\u{1d17a}'
  + '\\u{e0000}-\\u{e007f}\\u{e0100}-\\u{e01ef}]'
const NEEDS_ESCAPE = new RegExp(NEEDS_ESCAPE_SOURCE, 'u')
const PICTOGRAPHIC = /\p{Extended_Pictographic}/u
const EMOJI_MODIFIER = /\p{Emoji_Modifier}/u
// Scripts whose words need ZWNJ or ZWJ (Persian is Arabic script).
const JOINING_SCRIPT = new RegExp('[' + ['Arabic', 'Devanagari', 'Bengali', 'Gurmukhi', 'Gujarati', 'Oriya', 'Tamil', 'Telugu', 'Kannada', 'Malayalam', 'Sinhala']
  .map(script => `\\p{scx=${script}}`).join('') + ']', 'u')
const LETTER_OR_MARK = /[\p{L}\p{M}]/u
const LETTER = /\p{L}/u
const MARK = /\p{M}/u
// A base keeps at most this many combining marks; the next ones are escaped.
const MAX_MARKS = 2

// Scripts written right to left (the script properties stand in for Bidi_Class R and AL, which a regex cannot name).
const RTL_SCRIPTS = ['Hebrew', 'Arabic', 'Syriac', 'Thaana', 'Nko', 'Adlam', 'Samaritan', 'Mandaic', 'Hanifi_Rohingya']
const RTL_LETTER = new RegExp('[' + RTL_SCRIPTS.map(script => `\\p{Script=${script}}`).join('') + ']', 'u')
const RTL_DIGIT = /[٠-٩۰-۹]/u
const RTL_ANY = new RegExp(RTL_LETTER.source.slice(0, -1) + '\\u0660-\\u0669\\u06f0-\\u06f9]', 'u')
// Text that may need an escape or an isolate: one cheap test that most lines fail.
const MAY_NEED = new RegExp(`${NEEDS_ESCAPE_SOURCE}|\\p{M}{${MAX_MARKS + 1}}|${RTL_ANY.source}`, 'u')
const MAY_HIDE = new RegExp(`${NEEDS_ESCAPE_SOURCE}|\\p{M}{${MAX_MARKS + 1}}`, 'u')

const isLetterOfJoiningScript = (char, pattern) => char !== undefined && pattern.test(char) && JOINING_SCRIPT.test(char)
// A selector or joiner that belongs to the text around it. chars: the code points; at: its index.
function belongsToText(chars, at, afterKeptSelector) {
  const code = chars[at].codePointAt(0)
  const prev = chars[at - 1], next = chars[at + 1]
  const afterEmoji = prev !== undefined && (PICTOGRAPHIC.test(prev) || EMOJI_MODIFIER.test(prev))
  if (code >= 0xfe00 && code <= 0xfe0f) return afterEmoji || (code === 0xfe0f && /^[0-9#*]$/.test(prev ?? '') && next === '⃣')
  const between = isLetterOfJoiningScript(prev, LETTER_OR_MARK) && isLetterOfJoiningScript(next, LETTER)
  if (code === 0x200c) return between
  if (code === 0x200d) return between || ((afterEmoji || afterKeptSelector) && next !== undefined && PICTOGRAPHIC.test(next))
  return false
}

// The first sentence names the escapes; the second says what right-to-left text gets.
export const HIDDEN_NOTE = 'Hidden or direction-changing characters are shown as \\u escapes.'
export const RTL_NOTE = `${HIDDEN_NOTE} Right-to-left text is kept in typing order.`
const escapeOf = code => code < 0x100 ? `\\x${code.toString(16).padStart(2, '0')}`
  : code > 0xffff ? `\\u{${code.toString(16)}}` : `\\u${code.toString(16).padStart(4, '0')}`

// What escaping one piece of text found: pieces ({ text } as it is, { text, escaped: true, code } for each escape,
// { text, rtl: true } for a run of right-to-left letters or of Arabic-Indic digits), the number of escapes (counting
// stops after stopAfter) and whether the text holds a right-to-left run. build: false skips the pieces.
function scan(text, { stopAfter = Infinity, build = true } = {}) {
  const chars = Array.from(String(text ?? ''))
  const pieces = []
  let escapes = 0, rtl = false, markRun = 0, afterKeptSelector = false
  let open = null  // The kind of the last piece while it can still grow: 'text', 'letters' or 'digits'.
  const add = (kind, char) => {
    if (build) {
      if (kind === open) pieces[pieces.length - 1].text += char
      else pieces.push(kind === 'text' ? { text: char } : { text: char, rtl: true })
    }
    open = kind
  }
  for (let at = 0; at < chars.length; at++) {
    const char = chars[at]
    const code = char.codePointAt(0)
    const isMark = MARK.test(char)
    markRun = isMark ? markRun + 1 : 0
    const hide = !SHOWN_AS_IS.has(code) && ((NEEDS_ESCAPE.test(char) && !belongsToText(chars, at, afterKeptSelector))
      || (isMark && markRun > MAX_MARKS))
    afterKeptSelector = !hide && code >= 0xfe00 && code <= 0xfe0f
    if (hide) {
      escapes++
      if (build) pieces.push({ text: escapeOf(code), escaped: true, code })
      open = null
      if (escapes > stopAfter) break
      continue
    }
    if (RTL_LETTER.test(char)) { rtl = true; add('letters', char) }
    else if (RTL_DIGIT.test(char)) { rtl = true; add('digits', char) }
    // A mark stays with the run it follows; a joiner kept between two right-to-left letters keeps the word whole.
    else if (isMark && (open === 'letters' || open === 'digits')) add(open, char)
    else if ((code === 0x200c || code === 0x200d) && open === 'letters' && RTL_LETTER.test(chars[at + 1] ?? '')) add('letters', char)
    else add('text', char)
  }
  return { pieces, escapes, rtl }
}

// The text as pieces: { text } for what is shown as it is, { text, escaped: true, code } for each escape and
// { text, rtl: true } for each run of right-to-left letters (kept in the order typed; see visibleParts).
export const visibleSegments = text => scan(text).pieces
// "hidden character U+202E": the tooltip and the label of one escape.
export const hiddenCharTitle = code => `hidden character U+${code.toString(16).toUpperCase().padStart(4, '0')}`
// An escape is its own element, so a real character and the same characters typed (a backslash and "u202e")
// look different: the escape has a badge look (rule-badge rule-ask) and a tooltip.
export const HIDDEN_CHAR_CLASS = 'hidden-char rule-badge rule-ask'
export const RTL_RUN_CLASS = 'rtl-run'

// A last resort that cannot fail on odd input: every character outside printable ASCII, newline and tab becomes an escape.
function blunt(text) {
  let out = ''
  try { text = String(text ?? '') } catch { return '' }
  for (const char of text) {
    const code = char.codePointAt(0)
    out += (code >= 0x20 && code < 0x7f) || code === 0x0a || code === 0x09 ? char : escapeOf(code)
  }
  return out
}

// The plain string, for attributes (title, aria-label) and for places that cannot hold elements (an option). Right-to-left
// text stays as typed here: an attribute is not where the decision is made. Never throws: on failure every character
// outside printable ASCII is escaped.
export function visibleText(text) {
  try { return scan(text).pieces.map(piece => piece.text).join('') } catch { return blunt(text) }
}
// The same as text and one element for each escape and each right-to-left run, for what a person reads. Put it in an element
// with the class logical-order. Never throws: on failure the plain escaped string is returned.
export function visibleParts(text) {
  try {
    return scan(text).pieces.map(piece => piece.escaped
      ? html`<span class=${HIDDEN_CHAR_CLASS} title=${hiddenCharTitle(piece.code)}>${piece.text}</span>`
      : piece.rtl ? html`<bdi class=${RTL_RUN_CLASS}>${piece.text}</bdi>` : piece.text)
  } catch { return [blunt(text)] }
}
// Whether visibleText would change this text (fails closed: true when escaping fails).
export function hasHidden(text) {
  try {
    const value = String(text ?? '')
    return MAY_HIDE.test(value) && scan(value, { stopAfter: 0, build: false }).escapes > 0
  } catch { return true }
}
// How many escapes this text needs, counted up to limit + 1 (so a huge text costs little).
export function countHidden(text, limit = Infinity) {
  try { return scan(text, { stopAfter: limit, build: false }).escapes } catch { return limit + 1 }
}
// Whether the text holds a right-to-left run (visibleParts then isolates it).
// A right-to-left character that is itself escaped (U+0600) is no run, so the text is scanned once one is found.
export const hasRtl = text => {
  try { const value = String(text ?? ''); return RTL_ANY.test(value) && scan(value, { build: false }).rtl } catch { return true }
}
// Whether the text needs visibleParts to read as it is: an escape or a right-to-left run.
export const needsParts = text => { try { return MAY_NEED.test(String(text ?? '')) && (hasHidden(text) || hasRtl(text)) } catch { return true } }

// The note a card shows once for the texts a person reads: HIDDEN_NOTE when something is escaped, RTL_NOTE when a
// right-to-left run is present, null for neither. texts: an array (or one string); other values are ignored.
export function hiddenNote(texts) {
  let escaped = false
  for (const text of Array.isArray(texts) ? texts : [texts]) {
    if (typeof text !== 'string') continue
    if (hasRtl(text)) return RTL_NOTE
    if (!escaped && hasHidden(text)) escaped = true
  }
  return escaped ? HIDDEN_NOTE : null
}
// The element of a note from hiddenNote, or nothing.
export const noteLine = note => note ? html`<p class="tool-uncertain" role="note">${note}</p>` : null

// A read-only echo of an editable value that holds hidden characters (the input keeps the real value); nothing
// when it holds none. noted: the page already says escapes are shown.
export function EscapedEcho({ value, label = 'Shown with escapes', noted = false }) {
  if (!hasHidden(value)) return null
  return html`<div class="hidden-echo logical-order"><p class="field-help">${label}: <code>${visibleParts(value)}</code></p>
    ${noted ? null : html`<p class="tool-uncertain" role="note">${HIDDEN_NOTE}</p>`}</div>`
}
