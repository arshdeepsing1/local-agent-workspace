import { html, useEffect, useRef, useState } from '../lib.js'
import { Check, ChevronDown, X } from '../icons.js'

const modes = [
  { value: 'auto', label: 'Auto', description: 'Accept edits and basic listings; ask for other commands' },
  { value: 'manual', label: 'Manual', description: 'Ask before file changes and commands' },
  { value: 'acceptEdits', label: 'Accept edits', description: 'Accept file edits; ask before commands' },
  { value: 'plan', label: 'Plan', description: 'Explore and plan; no changes or commands' },
  { value: 'bypassPermissions', label: 'Bypass permissions', description: 'Run tools and access folders without asking' },
]

// An "Allow for this chat" rule: { tool: 'run_command', command } | { tool: 'edits' } | { tool: <MCP tool> } | { tool: 'hook', hook }.
export function ruleLabel(rule) {
  if (rule.tool === 'run_command') return `Command: ${rule.command}`
  if (rule.tool === 'edits') return 'File edits'
  if (rule.tool === 'hook') return `Hook: ${rule.hook}`
  return `Tool: ${rule.tool}`
}

// The visible label. A command keeps its exact spacing, since a rule matches only that exact text.
function RuleText({ rule }) {
  if (rule.tool !== 'run_command') return ruleLabel(rule)
  return html`Command: <code>${rule.command}</code>`
}

export default function PermissionsMenu({ mode, busy, onChange, onError, onDone, rules = [], onRemoveRule }) {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const container = useRef(null)
  const trigger = useRef(null)
  const menu = useRef(null)
  // After a rule is removed, focus stays in the menu: on the remove button now at the same
  // place, the one before it, or the selected mode once no rules are left.
  const refocus = useRef(null)
  useEffect(() => {
    if (refocus.current === null || saving || !open) return
    const buttons = menu.current?.querySelectorAll('.allow-rule [role="menuitem"]') || []
    const target = buttons[Math.min(refocus.current, buttons.length - 1)] || menu.current?.querySelector('[aria-checked="true"]')
    refocus.current = null
    target?.focus()
  })
  useEffect(() => {
    if (!open) return
    menu.current?.querySelector('[aria-checked="true"]')?.focus()
    const outside = event => { if (!container.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('focusin', outside)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('focusin', outside) }
  }, [open])
  const choose = async value => {
    setSaving(true)
    try { await onChange(value); setOpen(false); onDone() }
    catch (e) { onError(e.message) }
    finally { setSaving(false) }
  }
  const remove = async (rule, index) => {
    refocus.current = index
    setSaving(true)
    try { await onRemoveRule(rule) }
    catch (e) { onError(e.message) }
    finally { setSaving(false) }
  }
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
        const items = Array.from(menu.current?.querySelectorAll('[role="menuitemradio"], [role="menuitem"]') || [])
        const index = items.indexOf(document.activeElement)
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
        items[next]?.focus()
      }
    }}>
      <div class="permissions-heading">Mode<button class="icon-button" aria-label="Close permissions" onClick=${() => { setOpen(false); onDone() }}><${X} size=${14} /></button></div>
      ${modes.map(m => html`<button key=${m.value} role="menuitemradio" aria-checked=${mode === m.value} aria-disabled=${busy || saving}
        onClick=${() => { if (!busy && !saving) void choose(m.value) }}>
        <span><span class="permission-name">${m.label}${m.value === 'manual' ? html`<small>Default</small>` : null}</span>
          <span class="permission-description">${m.description}</span></span>
        ${mode === m.value ? html`<${Check} size=${16} className="permission-check" />` : null}
      </button>`)}
      ${rules.length && onRemoveRule ? html`<div class="allow-rules" role="group" aria-label="Allowed in this chat">
        <div class="permissions-heading">Allowed in this chat</div>
        ${rules.map((rule, index) => {
          const text = ruleLabel(rule)
          return html`<div class="allow-rule" key=${JSON.stringify(rule)}><span title=${text}><${RuleText} rule=${rule} /></span>
            <button role="menuitem" class="icon-button" aria-label=${`Remove rule: ${text}`} aria-disabled=${busy || saving}
              aria-describedby=${busy ? 'permissions-note' : undefined} title=${busy ? 'Stop the response before removing rules.' : undefined}
              onClick=${() => { if (!busy && !saving) void remove(rule, index) }}><${X} size=${13} /></button></div>`
        })}
      </div>` : null}
      <p class="permissions-note" id="permissions-note">${busy ? `Stop the response before changing modes${rules.length && onRemoveRule ? ' or removing rules' : ''}.` : 'Applies to this conversation. Auto uses local rules.'}</p>
    </div>` : null}
  </div>`
}
