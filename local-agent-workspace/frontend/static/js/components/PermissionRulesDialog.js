import { html, useEffect, useRef, useState } from '../lib.js'
import { X } from '../icons.js'

const BEHAVIORS = [['allow', 'Allow'], ['ask', 'Always ask'], ['deny', 'Deny']]
const SCOPES = [['chat', 'This chat'], ['project', 'This project'], ['user', 'All projects']]
const TARGETS = [['command', 'A command'], ['edits', 'File edits'], ['tool', 'A tool']]

// The draft sent to POST /permission-rules; the server parses and checks it (see types.js).
export function ruleDraft({ behavior, target, match, text, options, folder, tool }) {
  if (target === 'command') {
    return { tool: 'run_command', behavior, text: text.trim(), match,
      ...(match === 'prefix' && behavior === 'allow' && options.trim() ? { options: options.trim() } : {}) }
  }
  if (target === 'edits') return { tool: 'edits', behavior, ...(folder.trim() ? { path: folder.trim() } : {}) }
  return { tool: tool.trim(), behavior }
}

// Props: onAdd(scope, draft) -> Promise, onClose, returnFocus? (called when the dialog goes away)
export default function PermissionRulesDialog({ onAdd, onClose, returnFocus }) {
  const dialog = useRef(null)
  const [form, setForm] = useState({ behavior: 'allow', scope: 'project', target: 'command', match: 'exact', text: '', options: '', folder: '', tool: '' })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    const node = dialog.current
    node?.showModal()
    // The menu item that opened the dialog is gone, so focus goes back to where the caller says.
    return () => { if (node?.open) node.close(); returnFocus?.() }
  }, [])
  const set = (key, value) => { setForm(current => ({ ...current, [key]: value })); setError('') }
  const submit = async event => {
    event.preventDefault()
    if (saving) return
    setSaving(true); setError('')
    try { await onAdd(form.scope, ruleDraft(form)); onClose() }
    catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }
  const radio = (key, value, label) => html`<label class="choice" key=${value}><input type="radio" name=${`rule-${key}`} value=${value}
    checked=${form[key] === value} disabled=${saving} onChange=${() => set(key, value)} /><span>${label}</span></label>`
  const ready = form.target === 'command' ? !!form.text.trim() : form.target === 'tool' ? !!form.tool.trim() : true
  return html`<dialog ref=${dialog} class="settings-dialog permission-rules-dialog" aria-labelledby="permission-rules-title"
    onCancel=${onClose} onClick=${e => { if (e.target === dialog.current) onClose() }}>
    <form class="dialog-content" onSubmit=${submit}>
      <header><div><h2 id="permission-rules-title">Add a permission rule</h2>
        <p>Deny wins over Always ask, and Always ask over Allow. Plan, protected files and credential exclusions still apply.</p></div>
        <button type="button" class="icon-button" aria-label="Close" onClick=${onClose}><${X} size=${20} /></button></header>
      <label>Behavior<select disabled=${saving} value=${form.behavior} onChange=${e => set('behavior', e.currentTarget.value)}>
        ${BEHAVIORS.map(([value, label]) => html`<option key=${value} value=${value}>${label}</option>`)}</select></label>
      <fieldset><legend>Scope</legend>${SCOPES.map(([value, label]) => radio('scope', value, label))}</fieldset>
      <label>Applies to<select disabled=${saving} value=${form.target} onChange=${e => set('target', e.currentTarget.value)}>
        ${TARGETS.map(([value, label]) => html`<option key=${value} value=${value}>${label}</option>`)}</select></label>
      ${form.target === 'command' ? html`
        <fieldset><legend>Match</legend>${radio('match', 'exact', 'Exactly this command')}${radio('match', 'prefix', 'Commands starting with')}</fieldset>
        <label>Command<input required spellcheck=${false} disabled=${saving} value=${form.text} placeholder="git status" onInput=${e => set('text', e.currentTarget.value)} /></label>
        ${form.match === 'prefix' && form.behavior === 'allow' ? html`<label>Allowed options after it<input spellcheck=${false} disabled=${saving}
          value=${form.options} placeholder="--short" onInput=${e => set('options', e.currentTarget.value)} /></label>
          <p class="field-help">Separate options with spaces. Use --name= to allow any value. Nothing else may follow.</p>` : null}
        ${form.match === 'prefix' && form.behavior !== 'allow' ? html`<p class="field-help">Deny and Always ask rules match these words with any options.</p>` : null}` : null}
      ${form.target === 'edits' ? html`<label>Only under this folder<input spellcheck=${false} disabled=${saving} value=${form.folder}
        placeholder="/srv/project/src" onInput=${e => set('folder', e.currentTarget.value)} /></label>
        <p class="field-help">Optional. An absolute path; leave it empty for every file edit.</p>` : null}
      ${form.target === 'tool' ? html`<label>Tool name<input required spellcheck=${false} disabled=${saving} value=${form.tool}
        placeholder="mcp__docs__search" onInput=${e => set('tool', e.currentTarget.value)} /></label>
        <p class="field-help">For example mcp__docs__search, mcp__docs__*, delegate_task or remember (project memory).</p>` : null}
      ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
      <footer><button type="button" class="outline" onClick=${onClose}>Cancel</button>
        <button type="submit" class="primary" disabled=${saving || !ready}>Add rule</button></footer>
    </form>
  </dialog>`
}
