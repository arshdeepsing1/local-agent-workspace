import { html, useAutoFocus, useEffect, useRef, useState } from '../lib.js'
import { Check, ChevronDown, Pencil, X } from '../icons.js'
import { api } from '../api.js'
import { modeLabel, quoteArgv } from './PermissionsMenu.js'

// Pending-card fields (see types.js): rememberable, rule_options, editable, protected, uncertain, edited.
const SCOPE_LABELS = { chat: 'This chat', project: 'This project', user: 'All projects' }
const RULE_SCOPES = { chat: 'this chat', project: 'this project', user: 'all projects' }
const FIELD_LABELS = { command: 'Command', content: 'File content', new_text: 'Replacement text' }
// A shorter visible label for narrow cards. It must be the start of the accessible name (WCAG 2.5.3 Label in Name),
// so a speech-input user who says what they see still matches. Only the command label is long; the others
// ("Allow edits in this chat", "Allow subagents in this chat", …) keep their full text at every width.
const SHORT_ALLOW = { 'Always allow this command in this chat': 'Always allow this command' }

// The line under a card's heading that says why it ran without a card, or how its card was answered.
export function ruleLine(event) {
  const parts = []
  if (event.allowed_by_rule) parts.push(`Allowed by a rule for ${RULE_SCOPES[event.rule_scope] || 'this chat'}`)
  if (event.read_only_command) parts.push('Ran as a read-only command')
  if (event.approved_by_mode) parts.push(`Approved when you switched to ${modeLabel(event.approved_by_mode)}`)
  // A card edited and then declined, expired, blocked or cancelled was never approved.
  if (event.edited && event.state !== 'pending') parts.push(event.approval === 'approved' ? 'Edited by you before approval' : 'Edited by you')
  if (event.without_shell) parts.push(parts.length ? 'ran without a shell' : 'Ran without a shell')
  return parts.join(' · ')
}

function Radio({ name, value, checked, disabled, onChange, children }) {
  return html`<label class="choice"><input type="radio" name=${name} value=${value} checked=${checked} disabled=${disabled}
    onChange=${() => onChange(value)} /><span>${children}</span></label>`
}

// The "Always allow…" form: where the rule is saved and what it matches. Every choice comes from
// the server (rememberable and rule_options); the client never widens a rule on its own.
function RememberOptions({ event, disabled, onApprove }) {
  const options = event.rule_options || {}
  const isCommand = event.name === 'run_command' && 'argv' in options
  const legacyOnly = isCommand && !Array.isArray(options.argv)
  const offered = (event.rememberable || []).filter(scope => SCOPE_LABELS[scope] && (!legacyOnly || scope === 'chat'))
  const [scope, setScope] = useState(offered.includes('project') ? 'project' : offered[0])
  const prefixes = isCommand && !legacyOnly && Array.isArray(options.prefixes) ? options.prefixes : []
  const [match, setMatch] = useState('exact')
  const [prefixIndex, setPrefixIndex] = useState(0)
  const [flags, setFlags] = useState(() => (prefixes[0]?.flags || []).join(' '))
  const folderOption = typeof options.folder === 'string' && options.folder ? options.folder : null
  const [editsMatch, setEditsMatch] = useState('all')
  const [folder, setFolder] = useState(folderOption || '')
  const id = `remember-${event.id}`
  const submit = () => {
    if (legacyOnly) return onApprove({ remember: true })
    let rule
    if (isCommand) {
      const chosen = prefixes[prefixIndex]
      rule = match === 'prefix' && chosen
        ? { tool: 'run_command', behavior: 'allow', prefix: chosen.prefix, flags: flags.split(/\s+/).filter(Boolean) }
        : { tool: 'run_command', behavior: 'allow', argv: options.argv }
    } else if (folderOption) {
      rule = editsMatch === 'folder' ? { tool: 'edits', behavior: 'allow', path: folder.trim() } : { tool: 'edits', behavior: 'allow' }
    }
    return onApprove(rule ? { scope, rule } : { scope })
  }
  return html`<div class="remember-options" role="group" aria-label="Remember this approval">
    ${legacyOnly ? html`<p>${options.reason ? html`${options.reason} ` : null}Only this exact text can be allowed, in this chat.</p>` : null}
    <fieldset><legend>Where</legend>
      ${offered.map(value => html`<${Radio} key=${value} name=${`${id}-scope`} value=${value} checked=${scope === value} disabled=${disabled} onChange=${setScope}>${SCOPE_LABELS[value]}</${Radio}>`)}
    </fieldset>
    ${isCommand && !legacyOnly ? html`<fieldset><legend>Match</legend>
      <${Radio} name=${`${id}-match`} value="exact" checked=${match === 'exact'} disabled=${disabled} onChange=${setMatch}>Exactly this command <code>${quoteArgv(options.argv)}</code></${Radio}>
      ${prefixes.length ? html`<${Radio} name=${`${id}-match`} value="prefix" checked=${match === 'prefix'} disabled=${disabled} onChange=${setMatch}>Commands starting with</${Radio}>
        ${match === 'prefix' ? html`<div class="remember-prefix">
          <label>Command start<select disabled=${disabled} value=${String(prefixIndex)} onChange=${e => {
            const index = Number(e.currentTarget.value)
            setPrefixIndex(index); setFlags((prefixes[index]?.flags || []).join(' '))
          }}>${prefixes.map((item, index) => html`<option key=${index} value=${String(index)}>${quoteArgv(item.prefix)}</option>`)}</select></label>
          <label>Allowed options after it<input spellcheck=${false} disabled=${disabled} value=${flags} onInput=${e => setFlags(e.currentTarget.value)} /></label>
          <p class="field-help">Separate options with spaces. Use --name= to allow any value. Nothing else may follow.</p>
        </div>` : null}` : null}
    </fieldset>` : null}
    ${folderOption ? html`<fieldset><legend>Match</legend>
      <${Radio} name=${`${id}-edits`} value="all" checked=${editsMatch === 'all'} disabled=${disabled} onChange=${setEditsMatch}>All file edits</${Radio}>
      <${Radio} name=${`${id}-edits`} value="folder" checked=${editsMatch === 'folder'} disabled=${disabled} onChange=${setEditsMatch}>Files under</${Radio}>
      ${editsMatch === 'folder' ? html`<label class="remember-folder">Folder<input spellcheck=${false} disabled=${disabled} value=${folder} onInput=${e => setFolder(e.currentTarget.value)} /></label>` : null}
    </fieldset>` : null}
    <div class="approval-buttons"><button class="primary" disabled=${disabled || !scope || (folderOption && editsMatch === 'folder' && !folder.trim())}
      onClick=${() => void submit()}><${Check} size=${15} />Approve and remember</button></div>
  </div>`
}

// Edit before approving: the server re-checks the edited action and pushes the card's new preview.
function EditAction({ event, sessionId, onError, onClose }) {
  const fields = (event.editable || []).filter(field => FIELD_LABELS[field])
  const current = field => typeof event.input?.[field] === 'string' ? event.input[field] : ''
  const [values, setValues] = useState(() => Object.fromEntries(fields.map(field => [field, current(field)])))
  const [saving, setSaving] = useState(false)
  const changed = fields.some(field => values[field] !== current(field))
  const first = useAutoFocus()
  const save = async () => {
    setSaving(true)
    try {
      await api(`/sessions/${sessionId}/approvals/${event.id}/edit`, 'POST', { input: values })
      onClose()
    } catch (e) { onError(e.message) }
    finally { setSaving(false) }
  }
  return html`<div class="approval-edit">
    ${fields.map((field, index) => html`<label key=${field}>${FIELD_LABELS[field]}<textarea ref=${index === 0 ? first : null} spellcheck=${false} disabled=${saving}
      rows=${field === 'command' ? 3 : 8} value=${values[field]} onInput=${e => { const value = e.currentTarget.value; setValues(current => ({ ...current, [field]: value })) }} /></label>`)}
    <div class="approval-buttons"><button class="outline" disabled=${saving} onClick=${onClose}>Cancel edit</button>
      <button class="primary" disabled=${saving || !changed} onClick=${() => void save()}>Save edit</button></div>
  </div>`
}

// The pending body of an approval card below its first sentence: notes, the preview (or the editor),
// the decline note, the buttons and the "Always allow…" form.
// onDecide(allowed, extra?): extra is merged into the answer ({ remember } or { scope, rule? }).
export default function ApprovalOptions({ event, sessionId, onError, deciding, onDecide, allowInChat, details, declineNote }) {
  const [editing, setEditing] = useState(false)
  const [remembering, setRemembering] = useState(false)
  const canRemember = !event.protected && (event.rememberable || []).length > 0
  const canEdit = (event.editable || []).some(field => FIELD_LABELS[field])
  const locked = deciding || editing
  // Keyboard focus moves into the editor when it opens and back to Edit when it closes.
  const editButton = useRef(null)
  const refocusEdit = useRef(false)
  useEffect(() => {
    if (editing || !refocusEdit.current) return
    refocusEdit.current = false
    editButton.current?.focus()
  }, [editing])
  const closeEditor = () => { refocusEdit.current = true; setEditing(false) }
  // The same for the "Always allow…" form opened from the split button's arrow: its first choice, then the arrow.
  const rememberToggle = useRef(null)
  const rememberForm = useRef(null)
  const toggledRemember = useRef(false)
  useEffect(() => {
    if (!toggledRemember.current) return
    toggledRemember.current = false
    if (remembering) rememberForm.current?.querySelector('input[type="radio"]')?.focus()
    else rememberToggle.current?.focus()
  }, [remembering])
  const split = !!allowInChat && canRemember
  const short = SHORT_ALLOW[allowInChat]
  return html`
    ${event.protected ? html`<p class="tool-protected">Protected file: ${event.protected}. Edits to it always need your approval.</p>` : null}
    ${event.uncertain ? html`<p class="tool-uncertain">This command could not be checked exactly against your deny and ask rules, so it needs your approval.</p>` : null}
    ${event.edited ? html`<p class="tool-edited">Edited by you</p>` : null}
    ${editing ? html`<${EditAction} event=${event} sessionId=${sessionId} onError=${onError} onClose=${closeEditor} />` : details}
    ${declineNote}
    <div class="approval-buttons"><button class="outline" disabled=${locked} onClick=${() => void onDecide(false)}><${X} size=${15} />Decline</button>
      ${canEdit && !editing ? html`<button ref=${editButton} class="outline" disabled=${deciding} onClick=${() => { setRemembering(false); setEditing(true) }}><${Pencil} size=${14} />Edit</button>` : null}
      ${split ? html`<span class="split-button" role="group" aria-label="Always allow">
        <button class="outline" disabled=${locked} aria-label=${allowInChat} title=${allowInChat} onClick=${() => void onDecide(true, { remember: true })}>
          ${short ? html`<span class="label-long">${allowInChat}</span><span class="label-short" aria-hidden="true">${short}</span>` : allowInChat}</button>
        <button ref=${rememberToggle} class="outline split-toggle" disabled=${locked} aria-label="Always allow…" aria-expanded=${remembering}
          onClick=${() => { toggledRemember.current = true; setRemembering(v => !v) }}><${ChevronDown} size=${14} /></button>
      </span>` : html`
      ${canRemember ? html`<button class="outline" disabled=${locked} aria-expanded=${remembering} onClick=${() => setRemembering(v => !v)}>Always allow…</button>` : null}
      ${allowInChat ? html`<button class="outline" disabled=${locked} onClick=${() => void onDecide(true, { remember: true })}>${allowInChat}</button>` : null}`}
      <button class="primary" disabled=${locked} onClick=${() => void onDecide(true)}><${Check} size=${15} />Approve</button></div>
    ${canRemember && remembering && !editing ? html`<div ref=${rememberForm}><${RememberOptions} event=${event} disabled=${locked} onApprove=${extra => onDecide(true, extra)} /></div>` : null}`
}
