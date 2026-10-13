import { html, useEffect, useRef, useState } from '../lib.js'
import { ArrowUp, ChevronDown, Folder, FolderPlus, Laptop, ListPlus, MessageCircleQuestion, Plus, Square, Terminal, X } from '../icons.js'
import { modelLabel } from '../types.js'
import PermissionsMenu from './PermissionsMenu.js'
import { useSlashMenu } from './SlashMenu.js'
import { mentionLabel, parseMentions, removeMention, useMentionMenu } from './MentionMenu.js'

// What the text in the message box does when sent: a message, a ! command or a /btw question.
export function messageKind(text) {
  if (text.startsWith('!')) return 'shell'
  if (/^\/btw\s/.test(text)) return 'side'
  return 'message'
}
const clip = (text, limit) => text.length > limit ? `${text.slice(0, limit - 1)}…` : text

// A chip shows the file name in full and shortens its folder first.
function chipLabel(item) {
  const label = mentionLabel(item)
  const slash = label.lastIndexOf('/')
  return html`<span class="mention-label">${slash > 0 ? html`<span class="mention-dir">${label.slice(0, slash + 1)}</span>` : null}<span class="mention-base">${label.slice(slash + 1)}</span></span>`
}

// Props: value, setValue, model, models, busy, disabled, modelSaving, onModel, workspace,
// worktree (Session.worktree of an app-managed worktree chat: the chip names its project and branch),
// mode, onMode, onProject, onFolders, onSend, onStop, onAttach, onError, loadSkills,
// onShell(command), onSideQuestion(question), loadFiles(query), pendingContext (Session.pending_context),
// rules (this chat), projectRules, userRules, rulesProblem, onLoadRules, onAddRule(returnFocus), onRemoveRule(scope, rule), onResetRules
export default function Composer(p) {
  const [sending, setSending] = useState(false)
  const [caret, setCaret] = useState(0)
  const input = useRef(null)
  const slash = useSlashMenu({ value: p.value, setValue: p.setValue, loadSkills: p.loadSkills })
  const mention = useMentionMenu({ value: p.value, setValue: p.setValue, caret, setCaret, input, loadFiles: p.loadFiles })
  useEffect(() => { input.current?.focus() }, [])
  const text = p.value.trim()
  const kind = messageKind(text)
  const multiline = kind === 'shell' && /[\r\n]/.test(text)
  // A ! command needs an idle chat and one line; a message or side question can be sent while busy.
  const blocked = kind === 'shell' && (p.busy || multiline || !text.slice(1).trim())
  // A side question shows above the box at once, so the box is free for the next message while it
  // is answered; when it fails, its text comes back into an empty box.
  const askSide = () => {
    const draft = p.value
    p.setValue(current => current === draft ? '' : current)
    input.current?.focus()
    p.onSideQuestion(text.replace(/^\/btw\s+/, '').trim()).catch(e => {
      p.setValue(current => current.trim() ? current : draft)
      p.onError(e.message)
    })
  }
  const submit = async () => {
    if (!text || sending || p.modelSaving || p.disabled || blocked) return
    if (kind === 'side') { askSide(); return }
    setSending(true)
    try {
      if (kind === 'shell') await p.onShell(text.slice(1).trim())
      else await p.onSend(text)
      p.setValue(current => current === p.value ? '' : current)
      input.current?.focus()
    }
    catch (e) { p.onError(e.message) }
    finally { setSending(false) }
  }
  const keyDown = e => {
    if (slash.keyDown(e) || mention.keyDown(e)) return
    if (e.key === 'Escape' && p.busy && !e.isComposing) { e.preventDefault(); p.onStop(); return }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); void submit() }
  }
  const changeModel = async model => {
    if (sending || p.modelSaving || p.busy || p.disabled) return
    try { await p.onModel(model) }
    catch (e) { p.onError(e.message) }
  }
  const models = Array.from(new Set([p.model, ...p.models]))
  const mentions = kind === 'message' ? parseMentions(p.value) : []
  const pending = p.pendingContext || []
  const hint = kind !== 'shell' ? null : multiline ? 'Shell mode runs one line.'
    : p.busy ? 'Wait for the reply to finish before running a command.'
    : 'Shell command: Enter runs it in the project folder, even in Plan mode. Its output is sent to the agent with your next message.'
  const folderName = path => (path || '').split('/').filter(Boolean).pop()
  // A worktree chat's folder is an app-internal ID: show the project it came from and its branch.
  const project = p.worktree?.branch ? `${folderName(p.worktree.source_workspace) || 'Project'} · ${p.worktree.branch}`
    : folderName(p.workspace) || 'Project'
  const caretFrom = e => setCaret(e.currentTarget.selectionStart ?? 0)
  // Two menus share the message box; the open one names its list.
  const menuProps = mention.listed ? { ...slash.inputProps, ...mention.inputProps } : slash.inputProps
  return html`<div class="composer-block">
    <div class="project-context">
      <span class="context-chip"><${Laptop} size=${15} />Local</span>
      <button class="context-chip project-chip" aria-label="Choose project" title=${p.workspace} onClick=${p.onProject}><${Folder} size=${15} /><span>${project}</span></button>
      <button class="context-chip folder-access-button" onClick=${p.onFolders}><${FolderPlus} size=${15} /><span>Folder access</span></button>
    </div>
    ${mentions.length || pending.length ? html`<div class="mention-chips">
      ${pending.map((item, index) => html`<span class="mention-chip shell-chip" key=${`shell-${index}`}><${Terminal} size=${13} />Output of ! ${clip(item.command || '', 40)} (exit ${item.exit_code ?? '?'}) goes with your next message</span>`)}
      ${mentions.map(item => html`<span class="mention-chip" key=${item.token} title=${`${mentionLabel(item)}\nA snapshot is attached when you send (files only)`}>${chipLabel(item)}
        <button class="icon-button" aria-label=${`Remove @${item.path}`} onClick=${() => { p.setValue(current => removeMention(current, item.token)); input.current?.focus() }}><${X} size=${12} /></button></span>`)}
    </div>` : null}
    <div class="composer">
      <textarea ref=${input} aria-label="Message" placeholder=${p.mode === 'plan' ? 'Describe what you want to plan' : 'Describe a task or ask a question'} value=${p.value}
        onInput=${e => { p.setValue(e.currentTarget.value); caretFrom(e) }} onKeyUp=${caretFrom} onClick=${caretFrom} rows=${1}
        ...${menuProps}
        onFocus=${() => { slash.inputProps.onFocus(); mention.inputProps.onFocus() }} onBlur=${() => { slash.inputProps.onBlur(); mention.inputProps.onBlur() }}
        onKeyDown=${keyDown} />
      ${slash.menu}${mention.menu}
      ${p.busy && text && kind === 'message' ? html`<button class="send-button queue-button" aria-label="Queue message" title="Sent to the agent after its current step" disabled=${sending || p.modelSaving || p.disabled} onClick=${() => void submit()}><${ListPlus} size=${17} /></button>` : null}
      ${p.busy && kind === 'side' ? html`<button class="send-button queue-button" aria-label="Ask side question" title="Answered beside the conversation, without adding to it" disabled=${!text.replace(/^\/btw\s+/, '').trim() || p.modelSaving || p.disabled} onClick=${() => void submit()}><${MessageCircleQuestion} size=${17} /></button>` : null}
      ${p.busy ? html`<button class="send-button stop-button" aria-label="Stop response" onClick=${p.onStop}><${Square} size=${15} fill="currentColor" /></button>` :
        html`<button class="send-button" aria-label="Send message" disabled=${!text || sending || p.modelSaving || p.disabled || blocked} onClick=${() => void submit()}><${ArrowUp} size=${19} /></button>`}
    </div>
    ${hint ? html`<p class="composer-hint shell-hint" role="status">${hint}</p>` : null}
    <div class="composer-toolbar">
      <button class="attach-button icon-button" aria-label="Add project file" onClick=${p.onAttach}><${Plus} size=${19} /></button>
      <${PermissionsMenu} mode=${p.mode} busy=${p.busy} locked=${p.disabled || p.modelSaving || sending} onChange=${p.onMode} onError=${p.onError} onDone=${() => input.current?.focus()}
        rules=${p.rules} projectRules=${p.projectRules} userRules=${p.userRules} problem=${p.rulesProblem} onOpen=${p.onLoadRules}
        onRemoveRule=${p.onRemoveRule} onAddRule=${p.onAddRule && (() => p.onAddRule(() => input.current?.focus()))} onReset=${p.onResetRules} />
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
