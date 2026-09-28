import { html, useCallback, useEffect, useRef, useState } from '../lib.js'
import { ArrowLeft, ChevronRight, File, Folder, GitBranch, Plus, RefreshCw, Save, Terminal, X } from '../icons.js'
import { api } from '../api.js'
import TerminalPanel from './TerminalPanel.js'

// file: { id, path, content, original } | null; setFile accepts a value or an updater.
export default function WorkspacePanel({ sessionId, workspace, file, setFile, onClose, onAttach }) {
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
  const openFile = async path => {
    if (dirty && !confirm('Discard your unsaved edits?')) return
    const request = ++openRequest.current
    setError('')
    try {
      const result = await api(`/file?path=${encodeURIComponent(path)}&${query}`)
      if (request === openRequest.current) setFile({ id: crypto.randomUUID(), path, content: result.content, original: result.content })
    } catch (e) { if (request === openRequest.current) setError(e.message) }
  }
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
          <button class="icon-button" onClick=${() => onAttach(file.path)} aria-label="Add this file to chat"><${Plus} size=${16} /></button>
          <button class="icon-button" onClick=${() => void save()} disabled=${!dirty || loading} aria-label="Save file"><${Save} size=${16} /></button>
        </div>
        <textarea class="code-editor" aria-label=${`Edit ${file.path}`} value=${file.content} spellcheck=${false} onInput=${e => setFile({ ...file, content: e.currentTarget.value })} />
        <div class="editor-status">${file.content.split('\n').length} lines<span>${dirty ? 'Unsaved changes' : 'Saved on disk'}</span></div>
      </div>` : html`<div class="file-browser">
        <div class="file-path"><button class="icon-button" aria-label="Parent folder" disabled=${directory === '.'} onClick=${() => setDirectory(directory.includes('/') ? directory.slice(0, directory.lastIndexOf('/')) : '.')}><${ArrowLeft} size=${15} /></button><span>${directory === '.' ? 'Project files' : directory}</span>
          <button class="icon-button" aria-label="Refresh files" disabled=${loading} onClick=${() => void refresh()}><${RefreshCw} size=${14} className=${loading ? 'spin' : ''} /></button></div>
        <div class="file-list">${files.map(entry => html`<button key=${entry.path} class="file-row" onClick=${() => entry.directory ? setDirectory(entry.path) : void openFile(entry.path)}>
          ${entry.directory ? html`<${Folder} size=${16} />` : html`<${File} size=${16} />`}<span>${entry.name}</span>${entry.directory ? html`<${ChevronRight} size=${13} />` : null}
        </button>`)}${!loading && files.length === 0 ? html`<p class="panel-empty">This folder is empty.</p>` : null}</div>
        <p class="panel-footnote">Secret files and generated folders are excluded.</p>
      </div>`)
    : tab === 'changes' ? html`<div class="changes-view"><div class="file-path"><span>Working tree</span><button class="icon-button" aria-label="Refresh changes" onClick=${() => void refresh()}><${RefreshCw} size=${15} className=${loading ? 'spin' : ''} /></button></div>
      <pre class="diff-output">${changes.split('\n').map((line, i) => html`<span key=${i} class=${line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : ''}>${line}${'\n'}</span>`)}</pre></div>`
    : html`<${TerminalPanel} sessionId=${sessionId} workspace=${workspace} />`}
  </aside>`
}
