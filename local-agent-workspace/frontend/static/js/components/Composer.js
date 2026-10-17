import { html, useEffect, useRef, useState } from '../lib.js'
import { ArrowUp, ChevronDown, FileText, Folder, FolderPlus, Image as ImageIcon, Laptop, ListPlus, MessageCircleQuestion, Paperclip, Plus, Square, Terminal, Type, X } from '../icons.js'
import { modelLabel } from '../types.js'
import PermissionsMenu from './PermissionsMenu.js'
import EffortPicker from './EffortPicker.js'
import { useSlashMenu } from './SlashMenu.js'
import { mentionLabel, parseMentions, removeMention, useMentionMenu } from './MentionMenu.js'
import { MAX_MESSAGE_LENGTH } from '../reviewComments.js'
import { parseCommand } from '../commands.js'
import { ACCEPT, MAX_MESSAGE_FILES, MAX_MESSAGE_IMAGES, fileKind, fileProblem, recordDetail, uploadRefs } from '../attachments.js'

// What the text in the message box does when sent: a message, a ! command, a /btw question or a built-in
// command (commands.js), which runs in the page. Options: slashCommands (Settings: false sends typed / text
// as it is), picked (the built-in chosen in the / menu, which runs even then), plain (sent as plain text).
export function messageKind(text, { slashCommands = true, picked = null, plain = false } = {}) {
  if (plain) return 'message'
  if (text.startsWith('!')) return 'shell'
  if (/^\/btw\s/.test(text) && (slashCommands || picked === 'btw')) return 'side'
  const parsed = parseCommand(text)
  if (parsed && (slashCommands || picked === parsed.command.id)) return 'command'
  return 'message'
}
const clip = (text, limit) => text.length > limit ? `${text.slice(0, limit - 1)}…` : text

let nextUpload = 0

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
// rules (this chat), projectRules, userRules, rulesProblem, onLoadRules, onAddRule(returnFocus), onRemoveRule(scope, rule), onResetRules,
// effort (this chat's reasoning effort), onEffort(level) (allowed while busy), reasoningFormat and
// thinkingSummaries (Settings → Reasoning, for the picker's labels), onCommand(id, args) (a built-in
// command; rejects with the error to show), loadCommands (command templates for the / menu),
// slashCommands (Settings → slash commands), attachments and setAttachments(update) (the files attached to the next
// message: [{ key, name, kind, model, record? (UploadRecord, once uploaded), asPath? }], kept by the page per view),
// onUpload(file) -> Promise<UploadRecord> (paste, drop and Attach files; absent: no attaching),
// plainRequest (true turns Send as plain text on: a queued plain-text message is back in the box) and onPlainShown()
export default function Composer(p) {
  const [sending, setSending] = useState(false)
  const [caret, setCaret] = useState(0)
  // Send as plain text: the next message goes as typed, without a command, skills, @ files or ! shell.
  const [plain, setPlain] = useState(false)
  useEffect(() => { if (p.plainRequest) { setPlain(true); p.onPlainShown?.() } }, [p.plainRequest])
  // A built-in command chosen in the / menu: it runs while the box still starts with it (slash commands off).
  const [picked, setPicked] = useState(null)
  const input = useRef(null)
  const picker = useRef(null)
  const [dragging, setDragging] = useState(false)
  const files = p.attachments || []
  const setFiles = p.setAttachments || (() => {})
  const uploading = files.some(item => !item.record)
  // Each file is checked here first (type and size), then uploaded at once; its chip shows it uploading, then its
  // type, size and estimated tokens. A refused file leaves no chip: the error says why.
  const attach = list => {
    if (!p.onUpload || p.disabled) return
    let count = files.length
    let images = files.filter(item => item.kind === 'image').length
    for (const file of [...(list || [])]) {
      const problem = fileProblem(file)
      const kind = fileKind(file)
      if (problem) { p.onError(problem); continue }
      if (count >= MAX_MESSAGE_FILES) { p.onError(`Attach at most ${MAX_MESSAGE_FILES} files to one message.`); break }
      if (kind === 'image' && images >= MAX_MESSAGE_IMAGES) { p.onError(`Attach at most ${MAX_MESSAGE_IMAGES} images to one message.`); continue }
      count += 1
      images += kind === 'image' ? 1 : 0
      const key = `upload-${++nextUpload}`
      setFiles(current => [...current, { key, name: file.name, kind, model: p.model }])
      p.onUpload(file).then(record => setFiles(current => current.some(item => item.key !== key && item.record?.id === record.id)
        ? current.filter(item => item.key !== key)  // The same file twice: one chip.
        : current.map(item => item.key === key ? { ...item, name: record.name, record } : item)))
        .catch(error => { setFiles(current => current.filter(item => item.key !== key)); p.onError(error.message) })
    }
  }
  const removeFile = key => { setFiles(current => current.filter(item => item.key !== key)); input.current?.focus() }
  const asPath = (key, value) => setFiles(current => current.map(item => item.key === key ? { ...item, asPath: value } : item))
  // Pasted files attach; copied text pastes as text, also from an app that adds a picture of it (a spreadsheet).
  const paste = e => {
    const pasted = [...(e.clipboardData?.files || [])]
    if (!pasted.length || !p.onUpload || e.clipboardData.getData('text/plain')) return
    e.preventDefault()
    attach(pasted)
  }
  const dragsFiles = e => !!p.onUpload && !p.disabled && [...(e.dataTransfer?.types || [])].includes('Files')
  const dragOver = e => {
    if (!dragsFiles(e)) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
    setDragging(true)
  }
  const dragLeave = e => { if (!e.currentTarget.contains(e.relatedTarget)) setDragging(false) }
  const drop = e => {
    setDragging(false)
    if (!dragsFiles(e)) return
    e.preventDefault()
    attach(e.dataTransfer.files)
  }
  const slashCommands = p.slashCommands !== false
  const run = async (id, args = '') => {
    if (sending || p.modelSaving || p.disabled || !p.onCommand) return
    const draft = p.value
    setSending(true)
    try {
      await p.onCommand(id, args)
      p.setValue(current => current === draft ? '' : current)
      setPicked(null)
    } catch (e) { p.onError(e.message) } finally { setSending(false) }
  }
  const slash = useSlashMenu({ value: p.value, setValue: p.setValue, loadSkills: p.loadSkills, loadCommands: p.loadCommands,
    slashCommands, onRun: id => void run(id), onPick: setPicked, disabled: plain })
  const mention = useMentionMenu({ value: p.value, setValue: p.setValue, caret, setCaret, input, loadFiles: plain ? null : p.loadFiles })
  useEffect(() => { input.current?.focus() }, [])
  useEffect(() => { if (picked && !new RegExp(`^/${picked}(\\s|$)`).test(p.value)) setPicked(null) }, [p.value, picked])
  const text = p.value.trim()
  const kind = messageKind(text, { slashCommands, picked, plain })
  const parsed = kind === 'command' ? parseCommand(text) : null
  const multiline = kind === 'shell' && /[\r\n]/.test(text)
  // A ! command needs an idle chat and one line; a message or side question can be sent while busy. A message waits
  // for its attached files to upload.
  const blocked = (kind === 'shell' && (p.busy || multiline || !text.slice(1).trim())) || (kind === 'message' && uploading)
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
  // asPlain: Shift+Alt+Enter sends this message as plain text without the toggle.
  const submit = async (asPlain = plain) => {
    const sentKind = asPlain ? 'message' : kind
    if (!text || sending || p.modelSaving || p.disabled || (blocked && sentKind === 'shell')) return
    if (sentKind === 'message' && uploading) return
    if (sentKind === 'command') { await run(parsed.command.id, parsed.args); return }
    if (sentKind === 'side') { askSide(); return }
    // The server's limit counts code points; checked before sending, so no draft chat is created and
    // the text stays in the box.
    const chars = sentKind === 'message' ? [...text].length : 0
    if (chars > MAX_MESSAGE_LENGTH) {
      p.onError(`Messages are limited to ${MAX_MESSAGE_LENGTH.toLocaleString('en-US')} characters (this one has ` +
        `${chars.toLocaleString('en-US')}). Save long text such as a handoff as a file in the project and attach it ` +
        'with @ (or ask the agent to read it), so the agent can outline it and read the parts it needs.')
      return
    }
    // The attached files go with a message (also as plain text), not with a ! command; once sent, their chips go.
    const sent = sentKind === 'message' ? files.filter(item => item.record) : []
    const uploads = uploadRefs(sent)
    setSending(true)
    try {
      if (sentKind === 'shell') await p.onShell(text.slice(1).trim())
      else await p.onSend(text, asPlain || uploads.length ? { ...(asPlain ? { verbatim: true } : {}), ...(uploads.length ? { uploads } : {}) } : undefined)
      p.setValue(current => current === p.value ? '' : current)
      if (sent.length) {
        const keys = new Set(sent.map(item => item.key))
        setFiles(current => current.filter(item => !keys.has(item.key)))
      }
      if (asPlain) setPlain(false)
      input.current?.focus()
    }
    catch (e) { p.onError(e.message) }
    finally { setSending(false) }
  }
  const keyDown = e => {
    if (e.key === 'Enter' && e.shiftKey && e.altKey && !e.isComposing) { e.preventDefault(); void submit(true); return }
    if (!plain && (slash.keyDown(e) || mention.keyDown(e))) return
    if (e.key === 'Escape' && p.busy && !e.isComposing) { e.preventDefault(); p.onStop(); return }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); void submit() }
  }
  const changeModel = async model => {
    if (sending || p.modelSaving || p.busy || p.disabled) return
    try { await p.onModel(model) }
    catch (e) { p.onError(e.message) }
  }
  const models = Array.from(new Set([p.model, ...p.models]))
  const mentions = kind === 'message' && !plain ? parseMentions(p.value) : []
  const pending = p.pendingContext || []
  const hint = plain ? 'Plain text: sent exactly as typed, without commands, skills, @ files or ! shell.'
    : parsed ? (p.busy && !parsed.command.whileBusy ? `Stop the response first: /${parsed.command.id} runs when the conversation is idle.`
      : `/${parsed.command.id}: ${parsed.command.description} Enter runs it here; nothing is sent to the agent.`)
    : kind === 'message' && uploading ? 'Uploading the attached files…'
    : kind !== 'shell' ? null : multiline ? 'Shell mode runs one line.'
    : p.busy ? 'Wait for the reply to finish before running a command.'
    : 'Shell command: Enter runs it in the project folder, even in Plan mode. Its output is sent to the agent with your next message.'
  const folderName = path => (path || '').split('/').filter(Boolean).pop()
  // A worktree chat's folder is an app-internal ID: show the project it came from and its branch.
  const project = p.worktree?.branch ? `${folderName(p.worktree.source_workspace) || 'Project'} · ${p.worktree.branch}`
    : folderName(p.workspace) || 'Project'
  const caretFrom = e => setCaret(e.currentTarget.selectionStart ?? 0)
  // Two menus share the message box; the open one names its list. As plain text, neither opens.
  const menuProps = plain ? {} : mention.listed ? { ...slash.inputProps, ...mention.inputProps } : slash.inputProps
  // An image the endpoint chosen when it was attached does not take, and a PDF whose text the app cannot read
  // (without the documents extra), say so under the chips; such a PDF can go as a path instead.
  const warnings = files.filter(item => item.record && ((item.record.vision_refusal && item.model === p.model) || item.record.unreadable))
  return html`<div class=${`composer-block${dragging ? ' dragging-files' : ''}`} onDragOver=${dragOver} onDragLeave=${dragLeave} onDrop=${drop}>
    <div class="project-context">
      <span class="context-chip context-label" title="This conversation runs on this computer: commands and file edits use your local files."><${Laptop} size=${15} />Local</span>
      <button class="context-chip project-chip" aria-label="Choose project" title=${p.workspace} onClick=${p.onProject}><${Folder} size=${15} /><span>${project}</span></button>
      <button class="context-chip folder-access-button" onClick=${p.onFolders}><${FolderPlus} size=${15} /><span>Folder access</span></button>
    </div>
    ${mentions.length || pending.length || files.length ? html`<div class="mention-chips">
      ${pending.map((item, index) => html`<span class="mention-chip shell-chip" key=${`shell-${index}`}><${Terminal} size=${13} />Output of ! ${clip(item.command || '', 40)} (exit ${item.exit_code ?? '?'}) goes with your next message</span>`)}
      ${mentions.map(item => html`<span class="mention-chip" key=${item.token} title=${`${mentionLabel(item)}\nA snapshot is attached when you send (files only)`}>${chipLabel(item)}
        <button class="icon-button" aria-label=${`Remove @${item.path}`} onClick=${() => { p.setValue(current => removeMention(current, item.token)); input.current?.focus() }}><${X} size=${12} /></button></span>`)}
      ${files.map(item => html`<span class=${`mention-chip upload-chip${item.record ? '' : ' uploading'}`} key=${item.key}
        title=${`${item.name}\n${item.record ? (item.kind === 'image' ? 'Sent as an image with your next message' : item.asPath ? 'Its saved path goes with your next message' : 'Its text goes with your next message') : 'Uploading…'}`}>
        <${item.kind === 'image' ? ImageIcon : FileText} size=${13} /><span class="upload-name">${item.name}</span>
        <span class="upload-detail">${item.record ? `${recordDetail(item.record)}${item.asPath ? ' · as a path' : ''}` : 'Uploading…'}</span>
        <button class="icon-button" aria-label=${`Remove ${item.name}`} onClick=${() => removeFile(item.key)}><${X} size=${12} /></button></span>`)}
    </div>` : null}
    ${warnings.map(item => item.asPath ? html`<p class="attachment-note" key=${`note-${item.key}`}>${item.name} goes as a path: the agent gets where the file is saved, for its commands to read, not its text.
        <button class="outline" onClick=${() => asPath(item.key, false)}>Undo</button></p>`
      : html`<p class="attachment-warning" role="alert" key=${`warn-${item.key}`}><strong>${item.name}:</strong> ${item.record.unreadable || item.record.vision_refusal}
        ${item.record.unreadable ? html`<button class="outline" onClick=${() => asPath(item.key, true)}>Attach as path</button>` : null}</p>`)}
    <div class="composer">
      <textarea ref=${input} aria-label="Message" placeholder=${p.mode === 'plan' ? 'Describe what you want to plan' : 'Describe a task or ask a question'} value=${p.value}
        onInput=${e => { p.setValue(e.currentTarget.value); caretFrom(e) }} onKeyUp=${caretFrom} onClick=${caretFrom} rows=${1}
        ...${menuProps}
        onFocus=${() => { slash.inputProps.onFocus(); mention.inputProps.onFocus() }} onBlur=${() => { slash.inputProps.onBlur(); mention.inputProps.onBlur() }}
        onKeyDown=${keyDown} onPaste=${paste} />
      ${plain ? null : html`${slash.menu}${mention.menu}`}
      ${p.busy && parsed ? html`<button class="send-button queue-button" aria-label="Run command" title="Runs here at once; nothing is sent to the agent"
        disabled=${sending || p.modelSaving || p.disabled || !parsed.command.whileBusy} onClick=${() => void submit()}><${ArrowUp} size=${17} /></button>` : null}
      ${p.busy && text && kind === 'message' ? html`<button class="send-button queue-button" aria-label="Queue message" title="Sent to the agent after its current step" disabled=${sending || p.modelSaving || p.disabled || uploading} onClick=${() => void submit()}><${ListPlus} size=${17} /></button>` : null}
      ${p.busy && kind === 'side' ? html`<button class="send-button queue-button" aria-label="Ask side question" title="Answered beside the conversation, without adding to it" disabled=${!text.replace(/^\/btw\s+/, '').trim() || p.modelSaving || p.disabled} onClick=${() => void submit()}><${MessageCircleQuestion} size=${17} /></button>` : null}
      ${p.busy ? html`<button class="send-button stop-button" aria-label="Stop response" onClick=${p.onStop}><${Square} size=${15} fill="currentColor" /></button>` :
        html`<button class="send-button" aria-label=${parsed ? 'Run command' : 'Send message'} disabled=${!text || sending || p.modelSaving || p.disabled || blocked} onClick=${() => void submit()}><${ArrowUp} size=${19} /></button>`}
    </div>
    ${hint ? html`<p class=${`composer-hint ${parsed || plain ? 'command-hint' : 'shell-hint'}`} role="status">${hint}</p>` : null}
    ${p.onUpload ? html`<input ref=${picker} type="file" multiple accept=${ACCEPT} hidden onChange=${e => { attach(e.currentTarget.files); e.currentTarget.value = '' }} />` : null}
    <div class="composer-toolbar">
      <button class="attach-button icon-button" aria-label="Add project file" onClick=${p.onAttach}><${Plus} size=${19} /></button>
      ${p.onUpload ? html`<button class="attach-button icon-button attach-files" aria-label="Attach files" disabled=${p.disabled}
        title="Attach documents (PDF, Word, Excel, PowerPoint, CSV, text, Markdown) or images to your next message; you can also paste or drop them"
        onClick=${() => picker.current?.click()}><${Paperclip} size=${16} /></button>` : null}
      <button class=${`icon-button plain-toggle${plain ? ' active' : ''}`} aria-label="Send as plain text" aria-pressed=${plain}
        title="Send as plain text: exactly as typed, without commands, skills, @ files or ! shell (or press Shift+Alt+Enter)"
        onClick=${() => { setPlain(value => !value); input.current?.focus() }}><${Type} size=${17} /></button>
      <${PermissionsMenu} mode=${p.mode} busy=${p.busy} locked=${p.disabled || p.modelSaving || sending} onChange=${p.onMode} onError=${p.onError} onDone=${() => input.current?.focus()}
        rules=${p.rules} projectRules=${p.projectRules} userRules=${p.userRules} problem=${p.rulesProblem} onOpen=${p.onLoadRules}
        onRemoveRule=${p.onRemoveRule} onAddRule=${p.onAddRule && (() => p.onAddRule(() => input.current?.focus()))} onReset=${p.onResetRules} />
      <span class="composer-spacer" />
      ${p.onEffort ? html`<${EffortPicker} model=${p.model} effort=${p.effort || 'default'} format=${p.reasoningFormat || 'auto'}
        summaries=${p.thinkingSummaries !== false} disabled=${p.disabled} onChange=${p.onEffort}
        onError=${p.onError} />` : null}
      <div class="model-control">
        <select aria-label="Model" value=${p.model} disabled=${p.disabled || p.busy || sending || p.modelSaving} onChange=${e => void changeModel(e.currentTarget.value)}
          title=${p.modelSaving ? 'Saving model…' : p.busy ? 'Stop the response before changing models.' : 'Choose model'}>
          ${models.map(model => html`<option key=${model} value=${model}>${modelLabel(model)}</option>`)}
        </select><${ChevronDown} size=${12} />
      </div>
    </div>
  </div>`
}
