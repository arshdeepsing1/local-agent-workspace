import { html, useEffect, useRef, useState } from '../lib.js'
import { ArrowUp, ChevronDown, Folder, FolderPlus, Laptop, Plus, Square } from '../icons.js'
import { modelLabel } from '../types.js'
import PermissionsMenu from './PermissionsMenu.js'
import { useSlashMenu } from './SlashMenu.js'

// Props: value, setValue, model, models, busy, disabled, modelSaving, onModel, workspace,
// mode, onMode, onProject, onFolders, onSend, onStop, onAttach, onError, loadSkills
export default function Composer(p) {
  const [sending, setSending] = useState(false)
  const input = useRef(null)
  const slash = useSlashMenu({ value: p.value, setValue: p.setValue, loadSkills: p.loadSkills })
  useEffect(() => { input.current?.focus() }, [])
  const submit = async () => {
    if (!p.value.trim() || sending || p.modelSaving || p.busy || p.disabled) return
    setSending(true)
    try { await p.onSend(p.value.trim()); p.setValue(current => current === p.value ? '' : current); input.current?.focus() }
    catch (e) { p.onError(e.message) }
    finally { setSending(false) }
  }
  const changeModel = async model => {
    if (sending || p.modelSaving || p.busy || p.disabled) return
    try { await p.onModel(model) }
    catch (e) { p.onError(e.message) }
  }
  const models = Array.from(new Set([p.model, ...p.models]))
  return html`<div class="composer-block">
    <div class="project-context">
      <span class="context-chip"><${Laptop} size=${15} />Local</span>
      <button class="context-chip project-chip" aria-label="Choose project" title=${p.workspace} onClick=${p.onProject}><${Folder} size=${15} /><span>${p.workspace.split('/').filter(Boolean).pop() || 'Project'}</span></button>
      <button class="context-chip folder-access-button" onClick=${p.onFolders}><${FolderPlus} size=${15} /><span>Folder access</span></button>
    </div>
    <div class="composer">
      <textarea ref=${input} aria-label="Message" placeholder=${p.mode === 'plan' ? 'Describe what you want to plan' : 'Describe a task or ask a question'} value=${p.value}
        onInput=${e => p.setValue(e.currentTarget.value)} rows=${1} ...${slash.inputProps}
        onKeyDown=${e => { if (slash.keyDown(e)) return; if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); void submit() } }} />
      ${slash.menu}
      ${p.busy ? html`<button class="send-button stop-button" aria-label="Stop response" onClick=${p.onStop}><${Square} size=${15} fill="currentColor" /></button>` :
        html`<button class="send-button" aria-label="Send message" disabled=${!p.value.trim() || sending || p.modelSaving || p.disabled} onClick=${() => void submit()}><${ArrowUp} size=${19} /></button>`}
    </div>
    <div class="composer-toolbar">
      <button class="attach-button icon-button" aria-label="Add project file" onClick=${p.onAttach}><${Plus} size=${19} /></button>
      <${PermissionsMenu} mode=${p.mode} busy=${p.busy || p.disabled || sending || p.modelSaving} onChange=${p.onMode} onError=${p.onError} onDone=${() => input.current?.focus()} />
      <span class="composer-spacer" />
      <div class="model-control">
        <select aria-label="Model" value=${p.model} disabled=${p.disabled || p.busy || sending || p.modelSaving} onChange=${e => void changeModel(e.currentTarget.value)}
          title=${p.modelSaving ? 'Saving model…' : p.busy ? 'Stop the response before changing models.' : 'Choose model'}>
          ${models.map(model => html`<option key=${model} value=${model}>${modelLabel(model)}</option>`)}
        </select><${ChevronDown} size=${12} />
      </div>
    </div>
  </div>`
}
