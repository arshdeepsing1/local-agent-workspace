import { html, useCallback, useEffect, useRef, useState } from '../lib.js'
import { ArrowLeft, ChevronRight, File, FileDiff, Folder, GitBranch, Pencil, Plus, RefreshCw, Save, Terminal, X } from '../icons.js'
import { api } from '../api.js'
import TerminalPanel from './TerminalPanel.js'
import DiffReview from './DiffReview.js'

// file: { id, path, content, original, mode?: 'edit'|'review' } | null; setFile accepts a value or an updater.
// changes: the conversation's unreviewed agent changes (see ChangesBar); review: { path } opens
// that file's changes once, then onReviewOpened() clears it; onReview(action, files) keeps or undoes.
export default function WorkspacePanel({ sessionId, workspace, file, setFile, onClose, onAttach, changes: agentChanges, busy, review, onReviewOpened, onReview }) {
  const [tab, setTab] = useState('files')
  const [directory, setDirectory] = useState('.')
  const [files, setFiles] = useState([])
  const [changes, setChanges] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const openRequest = useRef(0)
  useEffect(() => () => { openRequest.current++ }, [])
  const query = sessionId ? `session_id=${sessionId}` : ''
  const refresh = useCallback(async () => {
    setLoading(true); setError('')
    try {
      if (tab === 'files') setFiles(await api(`/files?path=${encodeURIComponent(directory)}&${query}`))
      if (tab === 'changes') {
        const result = await api(`/git?${query}`)
        setChanges(result.output || 'Your working tree is clean.')
      }
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }, [directory, query, tab])
  useEffect(() => { void refresh() }, [refresh])
  const dirty = file && file.content !== file.original
  const changed = new Map((agentChanges?.files || []).map(item => [item.path, item]))
  const pending = file && changed.get(file.path)
  const reviewing = file?.mode === 'review' && pending
  const openFile = async (path, mode = changed.has(path) ? 'review' : 'edit') => {
    if (file?.path === path && mode === 'review') { setFile({ ...file, mode }); return }  // Keeps an unsaved draft.
    if (dirty && !confirm('Discard your unsaved edits?')) return
    const request = ++openRequest.current
    setError('')
    try {
      const result = await api(`/file?path=${encodeURIComponent(path)}&${query}`)
      if (request === openRequest.current) setFile({ id: crypto.randomUUID(), path, content: result.content, original: result.content, mode })
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
      if (request === openRequest.current) setFile(current => current?.id === file.id ? { ...current, content: result.content, original: result.content, mode: 'edit', missing: false } : current)
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
          <button class="icon-button" onClick=${() => onAttach(file.path)} aria-label="Add this file to chat"><${Plus} size=${16} /></button>
          <button class="icon-button" onClick=${() => void save()} disabled=${!dirty || loading || reviewing} aria-label="Save file"><${Save} size=${16} /></button>
        </div>
        ${reviewing ? html`<${DiffReview} sessionId=${sessionId} path=${file.path} version=${pending.current_hash} busy=${busy} onAction=${onReview} />` : html`
        <textarea class="code-editor" aria-label=${`Edit ${file.path}`} value=${file.content} spellcheck=${false} onInput=${e => setFile({ ...file, content: e.currentTarget.value })} />
        <div class="editor-status">${file.content.split('\n').length} lines<span>${dirty ? 'Unsaved changes' : 'Saved on disk'}</span></div>`}
      </div>` : html`<div class="file-browser">
        <div class="file-path"><button class="icon-button" aria-label="Parent folder" disabled=${directory === '.'} onClick=${() => setDirectory(directory.includes('/') ? directory.slice(0, directory.lastIndexOf('/')) : '.')}><${ArrowLeft} size=${15} /></button><span>${directory === '.' ? 'Project files' : directory}</span>
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
    : tab === 'changes' ? html`<div class="changes-view"><div class="file-path"><span>Working tree</span><button class="icon-button" aria-label="Refresh changes" onClick=${() => void refresh()}><${RefreshCw} size=${15} className=${loading ? 'spin' : ''} /></button></div>
      <pre class="diff-output">${changes.split('\n').map((line, i) => html`<span key=${i} class=${line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : ''}>${line}${'\n'}</span>`)}</pre></div>`
    : html`<${TerminalPanel} sessionId=${sessionId} workspace=${workspace} />`}
  </aside>`
}
