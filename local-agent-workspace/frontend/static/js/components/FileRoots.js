import { html, useEffect, useRef, useState } from '../lib.js'
import { Folder, FolderPlus, X } from '../icons.js'
import { api } from '../api.js'
import { plural } from './ChangesBar.js'

// True when an absolute path lies inside the absolute folder `root`.
export const inside = (path, root) => path.startsWith(root.endsWith('/') ? root : `${root}/`)
// The default code folder from Settings (coding_dir_status) as a folder every chat may use, or null when it is
// off, not usable, the project itself or inside the project. { path, missing }.
export function standingFolder(codingDir, workspace) {
  if (!codingDir || !['ready', 'missing'].includes(codingDir.state) || typeof codingDir.path !== 'string') return null
  if (codingDir.path === workspace || inside(codingDir.path, workspace)) return null
  return { path: codingDir.path, missing: codingDir.state === 'missing' }
}
const parentFolder = path => path.slice(0, path.lastIndexOf('/')) || '/'
const OUTSIDE_SUGGESTIONS = 3

// Visible names for absolute folders: the last folder name, with as many parent names as it takes to
// tell equal names apart (the full path stays in the title and the accessible name).
export function shortNames(paths) {
  const parts = paths.map(path => path.split('/').filter(Boolean))
  const depth = parts.map(() => 1)
  const name = index => parts[index].slice(-depth[index]).join('/')
  for (let longer = true; longer;) {
    longer = false
    const names = parts.map((_, index) => name(index))
    names.forEach((value, index) => {
      if (depth[index] < parts[index].length && names.some((other, at) => at !== index && other === value)) { depth[index]++; longer = true }
    })
  }
  return new Map(paths.map((path, index) => [path, depth[index] >= parts[index].length ? path : name(index)]))
}

// The project's folders added to Files (view only: they never change what the agent may access).
// folders: [{ path, missing }]; add(path) resolves to the added folder's absolute path.
export function useWorkspaceFolders(sessionId, workspace) {
  const [folders, setFolders] = useState([])
  const version = useRef(0)
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  useEffect(() => {
    const request = ++version.current
    setFolders([])
    // Without the list, Files still shows the project and granted folders.
    api(`/workspace-folders${query}`).then(result => { if (request === version.current) setFolders(result.folders) }, () => {})
    return () => { version.current++ }
  }, [query, workspace])
  const change = async (method, path) => {
    const request = ++version.current
    const result = await api(`/workspace-folders${query}`, method, { path })
    if (request === version.current) setFolders(result.folders)
    return result.added
  }
  return { folders, add: path => change('POST', path), remove: path => change('DELETE', path) }
}

// The roots of Workspace -> Files: the project, the conversation's granted folders and the
// folders added to Files, each with a change dot, plus "Add folder…" and a hint for changed
// files under none of them. changed: paths of unreviewed agent changes (relative = project).
// codingDir: Settings coding_dir_status; the default code folder is a root in every chat, without a remove button.
export default function FileRoots({ workspace, root, onChoose, granted, folders, onAdd, onRemove, changed, codingDir = null }) {
  const [adding, setAdding] = useState(false)
  const [path, setPath] = useState('')
  const [formError, setFormError] = useState('')
  const [error, setError] = useState('')
  const [hintError, setHintError] = useState('')
  const [saving, setSaving] = useState(false)
  const input = useRef(null)
  const toggle = useRef(null)
  const box = useRef(null)
  const opened = useRef(false)
  const focusRoot = useRef(null)  // The root button to focus once rendered, after an add from the hint or a remove.
  useEffect(() => {
    if (adding) input.current?.focus()
    else if (opened.current) toggle.current?.focus()  // Back to "Add folder…" after Cancel or Add.
    opened.current = adding
  }, [adding])
  useEffect(() => {
    if (focusRoot.current === null) return
    const target = [...(box.current?.querySelectorAll('.file-roots-root') ?? [])].find(button => button.dataset.root === focusRoot.current)
    focusRoot.current = null
    if (target && !target.disabled) target.focus()
    else toggle.current?.focus()
  })
  const added = new Map(folders.map(folder => [folder.path, folder]))
  const standing = standingFolder(codingDir, workspace)
  const other = path => !standing || path !== standing.path
  const roots = [
    { value: '.' },
    // Missing until the agent's first write there; a changed file inside it means it exists now.
    ...(standing ? [{ value: standing.path, standing: true,
      missing: standing.missing && !changed.some(item => item.startsWith('/') && inside(item, standing.path)) }] : []),
    ...granted.filter(other).map(folder => ({ value: folder, granted: true, added: added.has(folder) })),
    ...folders.filter(folder => !granted.includes(folder.path) && other(folder.path))
      .map(folder => ({ value: folder.path, added: true, missing: folder.missing })),
  ]
  const outsideProject = changed.filter(item => item.startsWith('/') && !inside(item, workspace))
  const covering = roots.filter(item => item.value !== '.' && (!item.missing || item.standing)).map(item => item.value)
  const outside = outsideProject.filter(item => !covering.some(folder => inside(item, folder)))
  // One per folder of those files; a folder inside another suggestion is covered by it.
  const parents = [...new Set(outside.map(parentFolder))]
  const suggestions = parents.filter(folder => !parents.some(other => other !== folder && inside(folder, other))).slice(0, OUTSIDE_SUGGESTIONS)
  useEffect(() => setHintError(''), [outside.join('\n')])  // A refusal belongs to the files it was about.
  const names = shortNames(roots.filter(item => item.value !== '.').map(item => item.value))
  const suggestionNames = shortNames(suggestions)
  const count = value => value === '.' ? changed.filter(item => !item.startsWith('/') || inside(item, workspace)).length
    : changed.filter(item => inside(item, value)).length

  const addFolder = async (value, report, focus = false) => {
    setSaving(true); report('')
    try {
      const folder = await onAdd(value)
      onChoose(folder)
      if (focus) focusRoot.current = folder
      return true
    } catch (e) { report(e.message); return false }
    finally { setSaving(false) }
  }
  const close = () => { setAdding(false); setPath(''); setFormError('') }
  const submit = async event => {
    event.preventDefault()
    if (path.trim() && await addFolder(path.trim(), setFormError)) close()
  }
  const remove = async folder => {
    setError('')
    // Afterwards, focus the next folder (or the previous one, or Project) instead of losing focus.
    const index = roots.findIndex(item => item.value === folder)
    const next = [...roots.slice(index + 1), ...roots.slice(0, index).reverse()].find(item => !item.missing)
    try {
      await onRemove(folder)
      focusRoot.current = next?.value ?? '.'
    } catch (e) { setError(e.message) }
  }

  return html`<div class="file-roots" ref=${box}>
    ${roots.length > 1 || outsideProject.length ? html`<div class="file-roots-list" role="group" aria-label="Folders in Files">
      ${roots.map(item => {
        const changes = item.missing ? 0 : count(item.value)
        const kind = item.value === '.' ? null : item.standing ? (item.missing ? 'Created on first write' : 'Default code folder')
          : item.missing ? 'Not found' : item.granted ? 'Allowed in this chat' : 'Added to Files'
        const prefix = item.standing ? 'Default code folder' : item.granted ? 'Allowed in this chat' : 'Added to Files'
        const label = [item.value === '.' ? 'Project' : `${prefix}: ${item.value}`,
          item.missing ? kind : null, changes ? plural(changes, 'changed file') : null].filter(Boolean).join(', ')
        const selected = root === item.value
        return html`<div key=${item.value} class=${`file-roots-item${selected ? ' selected' : ''}`}>
          <button class="file-roots-root" data-root=${item.value} aria-pressed=${selected} aria-label=${label} title=${item.value === '.' ? workspace : item.value}
            disabled=${item.missing} onClick=${() => onChoose(item.value)}>
            <${Folder} size=${13} /><span class="file-roots-name">${item.value === '.' ? 'Project' : names.get(item.value)}</span>
            ${kind ? html`<small>${kind}</small>` : null}
            ${changes ? html`<span class="change-dot" aria-hidden="true" />` : null}
          </button>
          ${item.added ? html`<button class="icon-button file-roots-remove" aria-label=${`Remove ${item.value} from Files`} title="Remove from Files"
            onClick=${() => void remove(item.value)}><${X} size=${12} /></button>` : null}
        </div>`
      })}
    </div>` : null}
    ${outside.length ? html`<div class="file-roots-outside">
      <span>${plural(outside.length, 'changed file')} outside these folders</span>
      ${suggestions.map(folder => html`<button key=${folder} class="outline" disabled=${saving} title=${folder} aria-label=${`Add ${folder}`}
        onClick=${() => void addFolder(folder, setHintError, true)}>Add ${suggestionNames.get(folder)}</button>`)}
      ${hintError ? html`<p class="form-error" role="alert">${hintError}</p>` : null}
    </div>` : null}
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    ${adding ? html`<form class="file-roots-form" onSubmit=${submit} onKeyDown=${e => { if (e.key === 'Escape') { e.preventDefault(); close() } }}>
      <label for="file-roots-path">Folder path</label>
      <div><input id="file-roots-path" ref=${input} value=${path} spellcheck=${false} autocomplete="off" placeholder="~/projects/other"
          onInput=${e => setPath(e.currentTarget.value)} />
        <button type="submit" class="primary" disabled=${saving || !path.trim()}>Add</button>
        <button type="button" class="outline" onClick=${close}>Cancel</button></div>
      <p class="field-help">Shows the folder in Files for this project, with dots for files the agent changed there. The agent still needs Folder access to use it.</p>
      ${formError ? html`<p class="form-error" role="alert">${formError}</p>` : null}
    </form>` : html`<button ref=${toggle} class="file-roots-add" onClick=${() => { setError(''); setAdding(true) }}><${FolderPlus} size=${14} />Add folder…</button>`}
  </div>`
}
