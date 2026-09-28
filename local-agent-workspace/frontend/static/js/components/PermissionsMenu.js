import { html, useEffect, useRef, useState } from '../lib.js'
import { Check, ChevronDown, X } from '../icons.js'

const modes = [
  { value: 'auto', label: 'Auto', description: 'Accept edits and basic listings; ask for other commands' },
  { value: 'manual', label: 'Manual', description: 'Ask before file changes and commands' },
  { value: 'acceptEdits', label: 'Accept edits', description: 'Accept file edits; ask before commands' },
  { value: 'plan', label: 'Plan', description: 'Explore and plan; no changes or commands' },
  { value: 'bypassPermissions', label: 'Bypass permissions', description: 'Run tools and access folders without asking' },
]

export default function PermissionsMenu({ mode, busy, onChange, onError, onDone }) {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const container = useRef(null)
  const trigger = useRef(null)
  const menu = useRef(null)
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
        const items = Array.from(menu.current?.querySelectorAll('[role="menuitemradio"]') || [])
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
      <p>${busy ? 'Stop the response before changing modes.' : 'Applies to this conversation. Auto uses local rules.'}</p>
    </div>` : null}
  </div>`
}
