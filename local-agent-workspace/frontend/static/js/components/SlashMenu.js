import { html, useEffect, useRef, useState } from '../lib.js'

// Skill commands in the message box: typing "/" at the start of a message lists
// the skills this conversation can use, typing more filters them, and choosing
// one inserts "/<skill-id> " so the request can follow it. The server treats a
// message that starts with a listed skill's command as selecting that skill.

let menus = 0

// The command typed so far, or null. Only a message that is just "/" and one
// word opens the menu, so paths such as /tmp/output.log are left alone.
export const slashQuery = value => /^\/([^\s/]*)$/.exec(value)?.[1] ?? null

// Skills whose ID or name contains the query; prefix matches first.
export function matchingSkills(skills, query) {
  const text = query.toLowerCase()
  const rank = skill => {
    const id = skill.id.toLowerCase()
    const name = (skill.name || '').toLowerCase()
    return id.startsWith(text) ? 0 : name.startsWith(text) ? 1 : id.includes(text) || name.includes(text) ? 2 : 3
  }
  return skills.map(skill => [rank(skill), skill]).filter(([score]) => score < 3)
    .sort((a, b) => a[0] - b[0]).map(([, skill]) => skill)
}

// Options: value, setValue, loadSkills (resolves to Skill[]). Returns props and a
// key handler for the message box, and the menu to render next to it.
export function useSlashMenu({ value, setValue, loadSkills }) {
  const [id] = useState(() => `slash-menu-${++menus}`)
  const [focused, setFocused] = useState(false)
  const [dismissed, setDismissed] = useState(null)
  const [skills, setSkills] = useState(null)
  const [error, setError] = useState('')
  const [highlight, setHighlight] = useState({ query: null, index: 0 })
  const load = useRef(loadSkills)
  load.current = loadSkills
  const menu = useRef(null)
  const query = slashQuery(value)
  const open = query !== null && focused && value !== dismissed
  // Escape closes the menu until the message changes.
  useEffect(() => { if (dismissed !== null && value !== dismissed) setDismissed(null) }, [value, dismissed])
  // Reload on each opening so newly added skills appear without a page refresh.
  useEffect(() => {
    if (!open) return
    let current = true
    setError('')
    load.current().then(list => { if (current) setSkills(list) }, e => { if (current) { setSkills(null); setError(e.message) } })
    return () => { current = false }
  }, [open])
  const matches = open && skills ? matchingSkills(skills, query) : []
  const index = highlight.query === query ? Math.min(highlight.index, Math.max(matches.length - 1, 0)) : 0
  useEffect(() => { if (open) menu.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }) }, [open, index, query])
  const choose = skill => setValue(`/${skill.id} `)
  const keyDown = event => {
    if (!open || event.isComposing) return false
    if (event.key === 'Escape') { event.preventDefault(); setDismissed(value); return true }
    const completes = (event.key === 'Enter' || event.key === 'Tab') && !event.shiftKey
    // Until the first list arrives, do not send a half-typed command or leave the box.
    if (!skills && !error && completes) { event.preventDefault(); return true }
    if (!matches.length) return false
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setHighlight({ query, index: (index + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length })
      return true
    }
    if (completes) { event.preventDefault(); choose(matches[index]); return true }
    return false
  }
  const listed = open && matches.length > 0
  const inputProps = {
    'aria-autocomplete': 'list', 'aria-haspopup': 'listbox',
    'aria-controls': listed ? id : undefined, 'aria-activedescendant': listed ? `${id}-${index}` : undefined,
    onFocus: () => setFocused(true), onBlur: () => setFocused(false),
  }
  const note = error || (!skills ? 'Loading skills…' : skills.length ? `No skill matches /${query}.`
    : 'No skills found. Add one at .agents/skills/<skill-id>/SKILL.md in your project.')
  // Keep focus in the message box while the pointer chooses an option.
  const view = open ? html`<div class="slash-menu" ref=${menu} onMouseDown=${e => e.preventDefault()}>
    <div class="slash-menu-heading">Skills</div>
    ${listed ? html`<div role="listbox" id=${id} aria-label="Skills">${matches.map((skill, i) => html`<div key=${skill.id} id=${`${id}-${i}`}
      role="option" aria-selected=${i === index} class="slash-option" onMouseMove=${() => { if (i !== index) setHighlight({ query, index: i }) }}
      onClick=${() => choose(skill)}>
      <span class="slash-command">/${skill.id}${skill.name && skill.name !== skill.id ? html`<small>${skill.name}</small>` : null}</span>
      ${skill.description ? html`<span class="slash-description">${skill.description}</span>` : null}
    </div>`)}</div>` : html`<p class="slash-menu-note" role=${error ? 'alert' : 'status'}>${note}</p>`}
  </div>` : null
  return { inputProps, keyDown, menu: view }
}
