import { html, useEffect, useRef, useState } from '../lib.js'
import { COMMANDS, SIDE_QUESTION } from '../commands.js'

// The / menu of the message box: typing "/" at the start of a message lists the app's commands, the
// command templates of the project and your own folders, and the skills this conversation can use;
// typing more filters them. Choosing a built-in command that takes nothing runs it; choosing anything
// else inserts "/<name> " so its arguments or the request can follow. A skill whose ID a command owns
// (reserved: the server's answer) is inserted as "/skill <id> ".

let menus = 0

// Built into the app, listed first: the side question, then the commands (commands.js).
export const BUILT_IN = [SIDE_QUESTION, ...COMMANDS].map(command => ({ ...command, builtIn: true }))

// The command typed so far, or null. Only a message that is just "/" and one
// word opens the menu, so paths such as /tmp/output.log are left alone.
export const slashQuery = value => /^\/([^\s/]*)$/.exec(value)?.[1] ?? null

// Skills whose ID or name contains the query; the ID typed in full, then prefix matches first.
export function matchingSkills(skills, query) {
  const text = query.toLowerCase()
  const rank = skill => {
    const id = skill.id.toLowerCase()
    const name = (skill.name || '').toLowerCase()
    return id === text && text ? -1 : id.startsWith(text) ? 0 : name.startsWith(text) ? 1 : id.includes(text) || name.includes(text) ? 2 : 3
  }
  return skills.map(skill => [rank(skill), skill]).filter(([score]) => score < 3)
    .sort((a, b) => a[0] - b[0]).map(([, skill]) => skill)
}

// What a chosen option puts in the box.
const inserted = item => item.kind === 'skill' && item.reserved ? `/skill ${item.id} ` : `/${item.id} `

// Options: value, setValue, loadSkills (resolves to Skill[]), loadCommands? (resolves to { commands }),
// slashCommands (Settings: false lists only the built-in commands), onRun(id) (a built-in command chosen
// that takes nothing, or chosen with Enter when typed in full), onPick(id) (a built-in command inserted,
// so the box runs it even with slash commands off), disabled (plain text: no menu). Returns props and a
// key handler for the message box, and the menu to render next to it.
export function useSlashMenu({ value, setValue, loadSkills, loadCommands, slashCommands = true, onRun, onPick, disabled = false }) {
  const [id] = useState(() => `slash-menu-${++menus}`)
  const [focused, setFocused] = useState(false)
  const [dismissed, setDismissed] = useState(null)
  const [skills, setSkills] = useState(null)
  const [templates, setTemplates] = useState({ commands: [], error: '' })
  const [error, setError] = useState('')
  const [highlight, setHighlight] = useState({ query: null, index: 0 })
  const load = useRef({ loadSkills, loadCommands })
  load.current = { loadSkills, loadCommands }
  const menu = useRef(null)
  const query = slashQuery(value)
  const open = !disabled && query !== null && focused && value !== dismissed
  // Escape closes the menu until the message changes.
  useEffect(() => { if (dismissed !== null && value !== dismissed) setDismissed(null) }, [value, dismissed])
  // Reload on each opening so newly added skills and commands appear without a page refresh.
  useEffect(() => {
    if (!open) return
    let current = true
    setError('')
    const commands = load.current.loadCommands ? load.current.loadCommands().then(
      result => ({ commands: result?.commands || [], error: '' }), e => ({ commands: [], error: e.message })) : null
    Promise.all([load.current.loadSkills(), commands]).then(([list, found]) => {
      if (current) { setSkills(list); setTemplates(found || { commands: [], error: '' }) }
    }, e => { if (current) { setSkills(null); setError(e.message) } })
    return () => { current = false }
  }, [open])
  const groups = !open ? [] : [
    { name: 'Commands', items: matchingSkills(BUILT_IN, query) },
    ...(slashCommands && skills ? [
      { name: 'Custom commands', items: matchingSkills(templates.commands.map(item => ({ ...item, id: item.name, kind: 'command' })), query) },
      { name: 'Skills', items: matchingSkills(skills.filter(skill => skill.user_invocable !== false).map(skill => ({ ...skill, kind: 'skill' })), query) },
    ] : []),
  ].filter(group => group.items.length)
  const matches = groups.flatMap(group => group.items)
  const index = highlight.query === query ? Math.min(highlight.index, Math.max(matches.length - 1, 0)) : 0
  useEffect(() => { if (open) menu.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }) }, [open, index, query])
  // how: 'enter', 'tab' or 'click'. A built-in that takes nothing runs at once (also typed in full with optional
  // arguments and Enter); Tab only inserts.
  const choose = (item, how) => {
    if (item.builtIn && how !== 'tab' && onRun && (item.args === 'none' || (item.args === 'optional' && how === 'enter' && query === item.id))) {
      onRun(item.id)
      return
    }
    setValue(inserted(item))
    if (item.builtIn) onPick?.(item.id)
  }
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
    if (completes) { event.preventDefault(); choose(matches[index], event.key === 'Tab' ? 'tab' : 'enter'); return true }
    return false
  }
  const listed = open && matches.length > 0
  const inputProps = {
    'aria-autocomplete': 'list', 'aria-haspopup': 'listbox',
    'aria-controls': listed ? id : undefined, 'aria-activedescendant': listed ? `${id}-${index}` : undefined,
    onFocus: () => setFocused(true), onBlur: () => setFocused(false),
  }
  const note = error || (!skills ? 'Loading skills…' : !matches.length ? `Nothing matches /${query}.`
    : skills.length || !slashCommands ? '' : 'No skills found. Add one at .agents/skills/<skill-id>/SKILL.md in your project.')
  const offCommands = !slashCommands && skills ? 'Slash commands are off in Settings: typed / text is sent as it is; choose a command here to run it.' : ''
  let position = 0
  const option = item => {
    const i = position++
    const label = item.kind === 'skill' && item.reserved ? `/skill ${item.id}` : `/${item.id}`
    const hint = item.hint || item.argument_hint
    const name = item.kind === 'command' ? (item.source === 'user' ? 'yours' : 'project') : item.name && item.name !== item.id ? item.name : ''
    return html`<div key=${`${item.kind || 'built-in'}-${item.id}`} id=${`${id}-${i}`} role="option" aria-selected=${i === index}
      class="slash-option" onMouseMove=${() => { if (i !== index) setHighlight({ query, index: i }) }} onClick=${() => choose(item, 'click')}>
      <span class="slash-command">${label}${hint ? html` <span class="slash-hint">${hint}</span>` : null}${name ? html`<small>${name}</small>` : null}</span>
      ${item.description ? html`<span class="slash-description">${item.description}</span>` : null}
    </div>`
  }
  // Keep focus in the message box while the pointer chooses an option.
  const view = open ? html`<div class="slash-menu" ref=${menu} onMouseDown=${e => e.preventDefault()}>
    ${listed ? html`<div role="listbox" id=${id} aria-label="Commands and skills">${groups.map(group => html`<div role="group" key=${group.name}
      aria-label=${group.name}><div class="slash-menu-heading" aria-hidden="true">${group.name}</div>${group.items.map(option)}</div>`)}</div>` : null}
    ${note ? html`<p class="slash-menu-note" role=${error ? 'alert' : 'status'}>${note}</p>` : null}
    ${templates.error && slashCommands ? html`<p class="slash-menu-note" role="alert">Custom commands: ${templates.error}</p>` : null}
    ${offCommands ? html`<p class="slash-menu-note">${offCommands}</p>` : null}
  </div>` : null
  return { inputProps, keyDown, menu: view }
}
