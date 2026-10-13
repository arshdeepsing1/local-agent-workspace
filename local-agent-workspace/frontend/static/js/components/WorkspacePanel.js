import { html, useCallback, useEffect, useRef, useState } from '../lib.js'
import { ArrowLeft, ChevronRight, Code2, File, FileDiff, Folder, GitBranch, Pencil, Plus, RefreshCw, Save, Terminal, X } from '../icons.js'
import { api } from '../api.js'
import FileRoots, { standingFolder, useWorkspaceFolders } from './FileRoots.js'
import TerminalPanel from './TerminalPanel.js'
import DiffReview, { DiffLine, UnsentComments } from './DiffReview.js'
import { DiffStat } from './ChangesBar.js'

// A link that opens an absolute path at a line in VS Code (vscode://file/<path>:<line>).
// Each path segment is encoded, so "#" or "?" in a name cannot start a fragment or query.
function VSCodeLink({ absolute, line }) {
  if (typeof absolute !== 'string' || !absolute.startsWith('/')) return null
  const path = absolute.split('/').map(encodeURIComponent).join('/')
  return html`<a class="icon-button" href=${`vscode://file${path}:${line || 1}`} aria-label="Open in VS Code" title="Open in VS Code"><${Code2} size=${16} /></a>`
}

const GIT_GROUPS = [['staged', 'Staged changes'], ['unstaged', 'Changes'], ['untracked', 'Untracked']]
const GIT_STATUS = {
  modified: ['M', 'Modified'], type_changed: ['T', 'Type changed'], added: ['A', 'Added'], deleted: ['D', 'Deleted'],
  renamed: ['R', 'Renamed'], copied: ['C', 'Copied'], conflicted: ['!', 'Conflicted'], untracked: ['U', 'Untracked'],
}
const HIDDEN_NOTES = { credential: 'Credential file: diff hidden', generated: 'Generated folder: diff not shown' }

// The line a diff's "Open file" goes to: the first changed line in the new file. For a
// deletion that is the line before the removed lines (or after them at the top of a file).
function changedLine(lines) {
  const index = lines.findIndex(line => line.kind === 'added' || line.kind === 'removed')
  if (index < 0) return lines.find(line => line.new)?.new || 1
  const next = lines.slice(index).find(line => line.kind !== 'removed')
  if (next?.kind === 'added') return next.new
  const before = lines[index - 1]
  return (before?.kind === 'context' ? before.new : next?.new) || 1
}

// Git's view of the project (GET /git/status), grouped like an editor's source control
// view, and a read-only diff of one file (GET /git/diff). Refreshes when shown, on
// Refresh, and when a response ends. onOpenFile(path, line) opens the file in the editor.
function GitChanges({ query, workspace, busy, onOpenFile }) {
  const [status, setStatus] = useState(null)
  const [selected, setSelected] = useState(null)  // { entry, diff?, error? }
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const latest = useRef(0)
  // Keyboard focus moves to Back when a diff opens, and to the row again on Back.
  const backButton = useRef(null)
  const list = useRef(null)
  const returnTo = useRef(null)
  useEffect(() => {
    if (selected) { backButton.current?.focus(); return }
    const target = returnTo.current
    returnTo.current = null
    if (target) [...(list.current?.querySelectorAll('button.git-row') || [])]
      .find(row => row.dataset.area === target.area && row.dataset.path === target.path)?.focus()
  }, [selected?.entry])
  const refresh = useCallback(async () => {
    const request = ++latest.current
    setLoading(true); setError('')
    try {
      const result = await api(`/git/status?${query}`)
      if (request === latest.current) setStatus(result)
    } catch (e) { if (request === latest.current) setError(e.message) }
    finally { if (request === latest.current) setLoading(false) }
  }, [query])
  useEffect(() => { void refresh() }, [refresh])
  const wasBusy = useRef(busy)
  useEffect(() => { if (wasBusy.current && !busy) void refresh(); wasBusy.current = busy }, [busy])
  const showDiff = async entry => {
    returnTo.current = entry
    setSelected({ entry })
    const params = new URLSearchParams({ path: entry.path, area: entry.area, ...(entry.orig_path ? { orig_path: entry.orig_path } : {}) })
    try {
      const diff = await api(`/git/diff?${params}${query ? `&${query}` : ''}`)
      setSelected(current => current?.entry === entry ? { entry, diff } : current)
    } catch (e) { setSelected(current => current?.entry === entry ? { entry, error: e.message } : current) }
  }
  if (selected) {
    const { entry, diff } = selected
    const firstLine = diff ? changedLine(diff.lines) : 1
    const exists = entry.status !== 'deleted'
    // An untracked file's status and area are both "Untracked": name it once.
    const statusName = GIT_STATUS[entry.status]?.[1] || entry.status
    const areaName = GIT_GROUPS.find(([area]) => area === entry.area)?.[1]
    return html`<div class="git-diff-view">
      <div class="editor-toolbar"><button ref=${backButton} class="icon-button" aria-label="Back to changes" title="Back to changes" onClick=${() => setSelected(null)}><${ArrowLeft} size=${16} /></button>
        <span title=${entry.orig_path ? `${entry.orig_path} → ${entry.path}` : entry.path}>${entry.path}</span>
        ${exists ? html`<button class="editor-mode" onClick=${() => onOpenFile(entry.path, firstLine)}><${File} size=${14} />Open file</button>
          <${VSCodeLink} absolute=${workspace.startsWith('/') ? `${workspace.replace(/\/+$/, '')}/${entry.path}` : null} line=${firstLine} />` : null}
      </div>
      <div class="diff-review-summary"><span>${statusName}</span>
        ${areaName !== statusName ? html`<span>${areaName}</span>` : null}
        ${diff ? html`<${DiffStat} added=${diff.added} removed=${diff.removed} />` : null}
        ${entry.orig_path ? html`<span class="diff-review-note">from ${entry.orig_path}</span>` : null}</div>
      ${selected.error ? html`<div class="panel-error" role="alert">${selected.error}</div>`
        : !diff ? html`<p class="panel-empty">Loading diff…</p>`
        : diff.binary ? html`<p class="panel-empty">Binary file: no text diff to show.</p>`
        : !diff.lines.length ? html`<p class="panel-empty">No line changes.</p>`
        : html`<div class="diff-review-lines git-diff-lines" role="region" aria-label=${`Diff of ${entry.path}`} tabindex="0">
          ${diff.lines.map((line, index) => html`<${DiffLine} key=${index} line=${line} />`)}</div>`}
      ${diff?.truncated ? html`<p class="panel-footnote">This diff is long: showing its first 512 KB.</p>` : null}
    </div>`
  }
  const files = status?.files || []
  return html`<div class="changes-view">
    <div class="file-path"><${GitBranch} size=${14} /><span title=${status?.branch || ''}>${status?.repository === false ? 'Working tree' : status?.branch || (status ? 'Detached HEAD' : 'Working tree')}</span>
      <button class="icon-button" aria-label="Refresh changes" disabled=${loading} onClick=${() => void refresh()}><${RefreshCw} size=${15} className=${loading ? 'spin' : ''} /></button></div>
    ${error ? html`<div class="panel-error" role="alert">${error}</div>` : null}
    <div class="git-groups" ref=${list}>
      ${status?.repository === false ? html`<p class="panel-empty">Not a Git repository.</p>`
        : status && !files.length ? html`<p class="panel-empty">Your working tree is clean.</p>`
        : GIT_GROUPS.map(([area, label]) => {
          const group = files.filter(entry => entry.area === area)
          return group.length ? html`<section key=${area} class="git-group" aria-label=${label}><h3>${label} (${group.length})</h3>
            ${group.map(entry => {
              const [letter, name] = GIT_STATUS[entry.status] || ['?', entry.status]
              const content = html`<span class=${`git-letter ${entry.status}`} title=${name} aria-label=${name}>${letter}</span>
                <span class="git-path" title=${entry.orig_path ? `${entry.orig_path} → ${entry.path}` : entry.path}>${entry.path}</span>`
              return entry.hidden ? html`<div key=${entry.path} class="git-row hidden" title=${HIDDEN_NOTES[entry.hidden]}>${content}<span class="git-hidden">${HIDDEN_NOTES[entry.hidden]}</span></div>`
                : html`<button key=${entry.path} class="git-row" data-area=${entry.area} data-path=${entry.path} onClick=${() => void showDiff(entry)}>${content}
                  ${Number.isInteger(entry.added) && Number.isInteger(entry.removed) ? html`<${DiffStat} added=${entry.added} removed=${entry.removed} />` : null}</button>`
            })}</section>` : null
        })}
      ${status?.truncated ? html`<p class="panel-empty">Git lists more files than shown here.</p>` : null}
    </div>
    <p class="panel-footnote">Git's view of the project, including changes made by commands. Credential files are listed without their contents.</p>
  </div>`
}

// file: { id, path, content, original, mode?: 'edit'|'review' } | null; setFile accepts a value or an updater.
// changes: the conversation's unreviewed agent changes (see ChangesBar); review: { path } opens
// that file's changes once, then onReviewOpened() clears it; onReview(action, files) keeps or undoes;
// onSelectSession(id) opens a chat that a review error names as blocking.
// onSendComments(message) sends the review's line comments to the agent (see DiffReview).
// allowedDirectories: the conversation's granted folders (absolute), offered as other roots of Files.
// codingDir: Settings coding_dir_status (the default code folder, a root of Files in every chat);
// pythonVenv: Settings python_venv_status (named in the Terminal).
// open: { path, line? } opens that file in the editor once, at that line, then onOpened() clears it.
export default function WorkspacePanel({ sessionId, workspace, allowedDirectories = [], codingDir = null, pythonVenv = null, file, setFile, onClose, onAttach, changes: agentChanges, busy, review, onReviewOpened, onReview, onSelectSession, onSendComments, open, onOpened }) {
  const [tab, setTab] = useState('files')
  // root '.' is the project; a granted or added folder is browsed by absolute paths and never above itself.
  const [root, setRoot] = useState('.')
  const [directory, setDirectory] = useState('.')
  const chooseRoot = value => { setRoot(value); setDirectory(value) }
  const added = useWorkspaceFolders(sessionId, workspace)
  const standing = standingFolder(codingDir, workspace)
  const roots = [...(standing ? [standing.path] : []), ...allowedDirectories,
    ...added.folders.filter(folder => !folder.missing).map(folder => folder.path)]
  useEffect(() => { if (root !== '.' && !roots.includes(root)) chooseRoot('.') }, [root, roots.join('\n')])
  const parentOf = path => root === '.' ? (path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '.')
    : path.slice(0, path.lastIndexOf('/')).length < root.length ? root : path.slice(0, path.lastIndexOf('/'))
  const [files, setFiles] = useState([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const openRequest = useRef(0)
  useEffect(() => () => { openRequest.current++ }, [])
  const query = sessionId ? `session_id=${sessionId}` : ''
  const refresh = useCallback(async () => {
    setLoading(true); setError('')
    try {
      if (tab === 'files') setFiles(await api(`/files?path=${encodeURIComponent(directory)}&${query}`))
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }, [directory, query, tab])
  useEffect(() => { void refresh() }, [refresh])
  const dirty = file && file.content !== file.original
  const changed = new Map((agentChanges?.files || []).map(item => [item.path, item]))
  const pending = file && changed.get(file.path)
  const reviewing = file?.mode === 'review' && pending
  // A line to select once that file is in the editor: { id, line }; lineMark: { id, line, selected }.
  const editor = useRef(null)
  const pendingLine = useRef(null)
  const [lineMark, setLineMark] = useState(null)
  const openFile = async (path, mode = changed.has(path) ? 'review' : 'edit', line) => {
    if (file?.path === path && mode === 'review') { setFile({ ...file, mode }); return }  // Keeps an unsaved draft.
    if (dirty && !confirm('Discard your unsaved edits?')) return
    const request = ++openRequest.current
    setError('')
    try {
      const result = await api(`/file?path=${encodeURIComponent(path)}&${query}`)
      if (request !== openRequest.current) return
      const id = crypto.randomUUID()
      pendingLine.current = line ? { id, line } : null
      setLineMark(null)
      setFile({ id, path, content: result.content, original: result.content, mode, absolute: result.absolute })
    } catch (e) {
      if (request !== openRequest.current) return
      // A file the agent deleted, or that a command made unreadable, still has a review.
      if (mode === 'review') setFile({ id: crypto.randomUUID(), path, content: '', original: '', mode, missing: true })
      else setError(e.message)
    }
  }
  // Show the text editor again with the file as it is on disk, keeping an unsaved draft.
  const editFile = async (quiet = false) => {
    if (!file) return
    if (dirty) { setFile({ ...file, mode: 'edit' }); return }
    if (file.missing && quiet) { setFile(null); return }
    const request = ++openRequest.current
    try {
      const result = await api(`/file?path=${encodeURIComponent(file.path)}&${query}`)
      if (request === openRequest.current) setFile(current => current?.id === file.id ? { ...current, content: result.content, original: result.content, mode: 'edit', missing: false, absolute: result.absolute } : current)
    } catch (e) {
      if (request !== openRequest.current) return
      setFile(current => current?.id === file.id ? null : current)
      // Undoing a file the agent created removes it: go back to the folder quietly.
      if (!quiet) setError(e.message)
    }
  }
  useEffect(() => {
    if (!review) return
    onReviewOpened?.()
    setTab('files')
    void openFile(review.path, 'review')
  }, [review])
  // A file opened from a reply or a tool card, at a line when one is given.
  useEffect(() => {
    if (!open) return
    onOpened?.()
    setTab('files')
    void openFile(open.path, 'edit', open.line)
  }, [open])
  // Select the requested line once its file is in the editor, and scroll it into view.
  useEffect(() => {
    const pending = pendingLine.current
    const textarea = editor.current
    if (!pending || pending.id !== file?.id || !textarea) return
    pendingLine.current = null
    // The textarea's value, which the selection indexes into, has CRLF line breaks as LF.
    const lines = textarea.value.split('\n')
    const line = Math.min(Math.max(pending.line, 1), lines.length)
    const start = lines.slice(0, line - 1).join('\n').length + (line > 1 ? 1 : 0)
    textarea.focus()
    textarea.setSelectionRange(start, start + lines[line - 1].length)
    const height = parseFloat(getComputedStyle(textarea).lineHeight) || 20
    textarea.scrollTop = Math.max(0, (line - 1) * height - textarea.clientHeight / 3)
    setLineMark({ id: file.id, line, selected: true })
  }, [file?.id, tab, file?.mode])
  const leaveLine = () => { if (lineMark?.selected) setLineMark({ ...lineMark, selected: false }) }
  const markedLine = lineMark && lineMark.id === file?.id ? lineMark : null
  // Once a reviewed file is kept or undone, return to its current text.
  const reviewed = file?.mode === 'review' && agentChanges && !pending
  useEffect(() => { if (reviewed) void editFile(true) }, [reviewed])
  const save = async () => {
    if (!file) return
    setLoading(true); setError('')
    try {
      await api('/file', 'PUT', { path: file.path, content: file.content, original: file.original, session_id: sessionId })
      setFile(current => current?.id === file.id && current.original === file.original
        ? { ...current, original: file.content } : current)
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }
  const tabButton = (name, Icon, label) => html`<button class=${tab === name ? 'active' : ''} onClick=${() => setTab(name)}><${Icon} size=${15} />${label}</button>`
  return html`<aside class="workspace-panel" aria-label="Workspace panel">
    <header><div><h2>Workspace</h2><p title=${workspace}>${workspace.split('/').filter(Boolean).pop()}</p></div>
      <button class="icon-button" aria-label="Close workspace" onClick=${onClose}><${X} size=${19} /></button></header>
    <nav class="workspace-tabs" aria-label="Workspace views">
      ${tabButton('files', Folder, 'Files')}${tabButton('changes', GitBranch, 'Changes')}${tabButton('terminal', Terminal, 'Terminal')}
    </nav>
    ${error ? html`<div class="panel-error" role="alert">${error}</div>` : null}
    ${tab === 'files' ? (file ? html`<div class="file-editor">
        <div class="editor-toolbar"><button class="icon-button" aria-label="Back to files" onClick=${() => { if (!dirty || confirm('Discard unsaved edits?')) setFile(null) }}><${ArrowLeft} size=${16} /></button>
          <span title=${file.path}>${file.path}${dirty ? ' •' : ''}</span>
          ${reviewing ? (file.missing ? null : html`<button class="editor-mode" onClick=${() => void editFile()}><${Pencil} size=${14} />Edit</button>`)
            : pending ? html`<button class="editor-mode pending" onClick=${() => setFile({ ...file, mode: 'review' })}><${FileDiff} size=${14} />Review changes</button>` : null}
          <${VSCodeLink} absolute=${file.absolute} line=${markedLine?.line} />
          <button class="icon-button" onClick=${() => onAttach(file.path)} aria-label="Add this file to chat"><${Plus} size=${16} /></button>
          <button class="icon-button" onClick=${() => void save()} disabled=${!dirty || loading || reviewing} aria-label="Save file"><${Save} size=${16} /></button>
        </div>
        ${reviewing ? html`<${DiffReview} key=${file.path} sessionId=${sessionId} path=${file.path} version=${pending.current_hash} busy=${busy} onAction=${onReview} onSelectSession=${onSelectSession}
          onSendComments=${onSendComments} />` : html`
        <textarea ref=${editor} class="code-editor" aria-label=${`Edit ${file.path}`} value=${file.content} spellcheck=${false}
          onMouseDown=${leaveLine} onKeyDown=${leaveLine} onInput=${e => { leaveLine(); setFile({ ...file, content: e.currentTarget.value }) }} />
        <div class="editor-status"><span>${file.content.split('\n').length} lines${markedLine?.selected ? ` · Line ${markedLine.line}` : ''}</span><span>${dirty ? 'Unsaved changes' : 'Saved on disk'}</span></div>`}
      </div>` : html`<div class="file-browser">
        <${FileRoots} workspace=${workspace} root=${root} onChoose=${chooseRoot} granted=${allowedDirectories} folders=${added.folders} codingDir=${codingDir}
          onAdd=${added.add} onRemove=${added.remove} changed=${[...changed.keys()]} />
        <div class="file-path"><button class="icon-button" aria-label="Parent folder" disabled=${directory === root} onClick=${() => setDirectory(parentOf(directory))}><${ArrowLeft} size=${15} /></button><span title=${directory === '.' ? workspace : directory}>${directory === '.' ? 'Project files' : directory}</span>
          <button class="icon-button" aria-label="Refresh files" disabled=${loading} onClick=${() => void refresh()}><${RefreshCw} size=${14} className=${loading ? 'spin' : ''} /></button></div>
        <div class="file-list">${files.map(entry => {
          // Like an editor's explorer: a dot on files the agent changed, and on folders containing them.
          const dot = entry.directory ? [...changed.keys()].some(path => path.startsWith(`${entry.path}/`)) : changed.has(entry.path)
          return html`<button key=${entry.path} class="file-row" onClick=${() => entry.directory ? setDirectory(entry.path) : void openFile(entry.path)}>
          ${entry.directory ? html`<${Folder} size=${16} />` : html`<${File} size=${16} />`}<span>${entry.name}</span>
          ${dot ? html`<span class="change-dot" role="img" aria-label=${entry.directory ? 'Contains changed files' : 'Changed by the agent'} title=${entry.directory ? 'Contains files the agent changed' : 'Changed by the agent: open it to review'} />` : null}
          ${entry.directory ? html`<${ChevronRight} size=${13} />` : null}
        </button>`
        })}${!loading && files.length === 0 ? html`<p class="panel-empty">This folder is empty.</p>` : null}</div>
        <p class="panel-footnote">Secret files and generated folders are excluded.</p>
      </div>`)
    : tab === 'changes' ? html`<${GitChanges} query=${query} workspace=${workspace} busy=${busy}
      onOpenFile=${(path, line) => { setTab('files'); void openFile(path, 'edit', line) }} />`
    : html`<${TerminalPanel} sessionId=${sessionId} workspace=${workspace} pythonVenv=${pythonVenv} />`}
    ${sessionId && !(tab === 'files' && reviewing) ? html`<${UnsentComments} sessionId=${sessionId} busy=${busy} onSendComments=${onSendComments} />` : null}
  </aside>`
}
