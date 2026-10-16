import { html, useEffect, useRef, useState } from '../lib.js'
import { Check, ChevronDown, Plus, X } from '../icons.js'

export const modes = [
  { value: 'auto', label: 'Auto', description: 'Accept edits and read-only commands; ask for other commands' },
  { value: 'manual', label: 'Manual', description: 'Ask before file changes and commands' },
  { value: 'acceptEdits', label: 'Accept edits', description: 'Accept file edits; ask before commands' },
  { value: 'dontAsk', label: "Don't ask", description: 'Run only what rules allow; block everything else without asking' },
  { value: 'plan', label: 'Plan', description: 'Explore and plan; read-only commands only' },
  { value: 'bypassPermissions', label: 'Bypass permissions', description: 'Run tools and access folders without asking; deny and ask rules still apply' },
]
export const modeLabel = value => modes.find(m => m.value === value)?.label || value

export const BYPASS_CONFIRM = 'Switch this chat to Bypass permissions?\n\nEvery action will run without asking, including commands and edits outside the project, and any approval cards waiting now will be approved. Deny rules, ask rules, credential exclusions and approvals a hook asks for still apply.'

// The commands Auto and Plan run without asking: the server's fixed list of exact forms (README: Read-only commands).
const READ_ONLY_NOTE = 'A fixed list of read-only command forms, such as pwd, ls -la, git status, git diff --stat, git log --oneline and git show; other arguments still ask'
const NEVER_ASKS = ['Reading, listing and searching files in the project and granted folders', 'Listing jobs and reading their output',
  'Tasks and skills', 'Questions and plan reviews from the agent']

// A token that needs no quoting in a shell; anything else is shown in single quotes.
const PLAIN_TOKEN = /^[A-Za-z0-9_@%+=:,./-]+$/
export const quoteToken = token => PLAIN_TOKEN.test(token) ? token : `'${String(token).replace(/'/g, `'\\''`)}'`
export const quoteArgv = argv => (argv || []).map(quoteToken).join(' ')

// A rule (see types.js): { behavior?, tool, command?, argv?, prefix?, flags?, path?, hook?, domain? }.
// Returns [text before the code, code or null, text after it]; ruleLabel joins them.
function ruleParts(rule) {
  const tool = rule.tool || ''
  if (tool === 'run_command') {
    if (typeof rule.command === 'string') return ['Command: ', rule.command, '']
    if (Array.isArray(rule.argv)) return ['Command: ', quoteArgv(rule.argv), '']
    if (Array.isArray(rule.prefix)) {
      const after = (rule.behavior || 'allow') !== 'allow' ? ' (any options)'
        : rule.flags?.length ? ` with options ${quoteArgv(rule.flags)}` : ' with no other arguments'
      return ['Commands starting with ', quoteArgv(rule.prefix), after]
    }
    return ['Every command', null, '']
  }
  if (tool === 'edits') return rule.path ? ['File edits under ', rule.path, ''] : ['File edits', null, '']
  if (tool === 'hook') return rule.hook ? [`Hook: ${rule.hook}`, null, ''] : ['Hooks', null, '']
  if (tool === 'delegate_task') return ['Subagents', null, '']
  if (tool === 'stop_job') return ['Stopping jobs', null, '']
  if (tool === 'remember') return ['Saving project memory', null, '']
  if (tool === 'web_fetch') {
    if (!rule.domain) return ['Web fetches', null, '']
    return ['Fetches from ', rule.domain, (rule.behavior || 'allow') === 'allow' ? '' : ' and its subdomains']
  }
  if (tool === 'web_search') return ['Web searches', null, '']
  const server = /^mcp__(.+)__\*$/.exec(tool)?.[1]
  if (server) return ['All tools of MCP server ', server, '']
  return [`Tool: ${tool}`, null, '']
}

export function ruleLabel(rule) {
  const [before, code, after] = ruleParts(rule)
  return `${before}${code ?? ''}${after}`
}

// The visible label. A command keeps its exact spacing, since an M1 rule matches only that exact text.
export function RuleText({ rule }) {
  const [before, code, after] = ruleParts(rule)
  return code === null ? before : html`${before}<code>${code}</code>${after}`
}

const BADGES = { deny: 'Deny', ask: 'Always ask' }
const SCOPES = [['chat', 'This chat'], ['project', 'This project'], ['user', 'All projects']]

// Props: mode, busy (a turn is running), locked (no changes possible now), onChange(mode, options?),
// onError, onDone, rules (this chat), projectRules, userRules, problem, onOpen (load rules),
// onRemoveRule(scope, rule), onAddRule, onReset
export default function PermissionsMenu({ mode, busy, locked, onChange, onError, onDone, rules = [], projectRules = [], userRules = [],
  problem = null, onOpen, onRemoveRule, onAddRule, onReset }) {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const container = useRef(null)
  const trigger = useRef(null)
  const menu = useRef(null)
  const blocked = !!locked || saving
  // After a rule is removed, focus stays in the menu: on the remove button now at the same
  // place, the one before it, or the selected mode once no rules are left. After a reset the
  // focused Reset button is gone (unless the reset failed): focus moves to the selected mode.
  const refocus = useRef(null)
  useEffect(() => {
    if (refocus.current === null || saving || !open) return
    if (refocus.current === 'reset') {
      refocus.current = null
      if (!menu.current?.contains(document.activeElement)) menu.current?.querySelector('[aria-checked="true"]')?.focus()
      return
    }
    const buttons = menu.current?.querySelectorAll('.allow-rule [role="menuitem"]') || []
    const target = buttons[Math.min(refocus.current, buttons.length - 1)] || menu.current?.querySelector('[aria-checked="true"]')
    refocus.current = null
    target?.focus()
  })
  useEffect(() => {
    if (!open) return
    // The menu opens upwards from the message box, which grows with the draft: its top stays below the top bar.
    const topbar = document.querySelector('.topbar')
    if (menu.current && topbar) {
      const room = menu.current.getBoundingClientRect().bottom - topbar.getBoundingClientRect().bottom - 8
      menu.current.style.maxHeight = `${Math.max(160, Math.min(560, Math.floor(room)))}px`
    }
    menu.current?.querySelector('[aria-checked="true"]')?.focus()
    const outside = event => { if (!container.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('focusin', outside)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('focusin', outside) }
  }, [open])
  useEffect(() => {
    if (open && onOpen) void Promise.resolve().then(onOpen).catch(e => onError(e.message))
  }, [open])
  const choose = async value => {
    // Bypass approves waiting cards too, so it is confirmed first (also for a chat not yet created).
    const bypass = value === 'bypassPermissions' && mode !== 'bypassPermissions'
    if (bypass && !window.confirm(BYPASS_CONFIRM)) return
    setSaving(true)
    try { await (bypass ? onChange(value, { confirm: true }) : onChange(value)); setOpen(false); onDone() }
    catch (e) { onError(e.message) }
    finally { setSaving(false) }
  }
  const act = async (operation, index = null) => {
    refocus.current = index
    setSaving(true)
    try { await operation() }
    catch (e) { onError(e.message) }
    finally { setSaving(false) }
  }
  const groups = { chat: rules, project: projectRules, user: userRules }
  let position = 0
  const label = modes.find(m => m.value === mode)?.label
  return html`<div class="permissions-control" ref=${container}>
    <button ref=${trigger} class="permission-trigger" aria-haspopup="menu" aria-expanded=${open}
      aria-label=${`Permissions: ${label}`} onClick=${() => setOpen(v => !v)}>
      <${ChevronDown} size=${13} /><span>${label}</span>
    </button>
    ${open ? html`<div class="permissions-menu" role="menu" aria-label="Permission mode" ref=${menu} onKeyDown=${event => {
      if (event.key === 'Escape') { event.preventDefault(); setOpen(false); trigger.current?.focus() }
      if (event.key === 'Tab') setOpen(false)
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault()
        const items = Array.from(menu.current?.querySelectorAll('[role="menuitemradio"], [role="menuitem"], .never-asks>summary') || [])
        const index = items.indexOf(document.activeElement)
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
        items[next]?.focus()
      }
    }}>
      <div class="permissions-heading">Mode<button class="icon-button" aria-label="Close permissions" onClick=${() => { setOpen(false); onDone() }}><${X} size=${14} /></button></div>
      ${modes.map(m => html`<button key=${m.value} role="menuitemradio" aria-checked=${mode === m.value} aria-disabled=${blocked}
        onClick=${() => { if (!blocked) void choose(m.value) }}>
        <span><span class="permission-name">${m.label}${m.value === 'manual' ? html`<small>Default</small>` : null}</span>
          <span class="permission-description">${m.description}</span></span>
        ${mode === m.value ? html`<${Check} size=${16} className="permission-check" />` : null}
      </button>`)}
      ${onRemoveRule ? SCOPES.filter(([scope]) => groups[scope]?.length).map(([scope, heading]) => html`<div class="allow-rules rule-group" key=${scope} role="group" aria-label=${heading}>
        <div class="permissions-heading">${heading}</div>
        ${groups[scope].map(rule => {
          const index = position++
          const text = ruleLabel(rule)
          const badge = BADGES[rule.behavior]
          return html`<div class="allow-rule" key=${JSON.stringify(rule)}><span title=${text}><${RuleText} rule=${rule} /></span>
            ${badge ? html`<span class=${`rule-badge rule-${rule.behavior}`}>${badge}</span>` : null}
            <button role="menuitem" class="icon-button" aria-label=${`Remove rule: ${text}`} aria-disabled=${blocked}
              onClick=${() => { if (!blocked) void act(() => onRemoveRule(scope, rule), index) }}><${X} size=${13} /></button></div>`
        })}
      </div>`) : null}
      ${problem ? html`<div class="rules-problem"><p role="alert">${problem} They are ignored until you reset them.</p>
        <button role="menuitem" class="outline" aria-disabled=${blocked} onClick=${() => { if (!blocked && onReset) void act(onReset, 'reset') }}>Reset saved rules</button></div>` : null}
      ${onAddRule ? html`<button role="menuitem" class="add-rule" aria-disabled=${!!locked}
        onClick=${() => { if (!locked) { setOpen(false); onAddRule() } }}><${Plus} size=${14} />Add a rule…</button>` : null}
      <details class="never-asks" onToggle=${event => {
        // The menu scrolls and the note below sticks to its bottom: show the opened list above the note.
        if (event.currentTarget.open && menu.current) menu.current.scrollTop = menu.current.scrollHeight
      }}><summary>Never asks</summary><ul>
        ${NEVER_ASKS.map(item => html`<li key=${item}>${item}</li>`)}
        ${mode === 'auto' || mode === 'plan' ? html`<li>${READ_ONLY_NOTE}</li>` : null}
      </ul></details>
      <p class="permissions-note" id="permissions-note">${busy ? "Changes apply to the agent's next action." : 'Applies to this conversation. Rules can also apply to this project or all projects.'}</p>
    </div>` : null}
  </div>`
}
