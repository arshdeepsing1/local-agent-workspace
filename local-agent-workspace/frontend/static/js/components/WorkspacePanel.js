import { html, useCallback, useEffect, useLayoutEffect, useRef, useState } from '../lib.js'
import { Activity, ArrowLeft, ChevronRight, Code2, Download, Eye, File, FileDiff, Folder, GitBranch, Maximize2, Minimize2, Minus, Pencil, Plus, RefreshCw, Save, Terminal, X } from '../icons.js'
import { api } from '../api.js'
import FileRoots, { standingFolder, useWorkspaceFolders } from './FileRoots.js'
import TerminalPanel from './TerminalPanel.js'
import ProgressPanel from './ProgressPanel.js'
import DiffReview, { DiffLine, UnsentComments } from './DiffReview.js'
import { DiffStat } from './ChangesBar.js'
import FilePreview, { downloadFile, opensInPreview, previewKind } from './FilePreview.js'

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

// The pull request chip's text: "PR #12 · open · 3/4 checks" (GET /git/pr: git_tools.pr_status).
export function prChipText(pr) {
  const checks = pr.checks?.total ? ` · ${pr.checks.passed}/${pr.checks.total} checks${pr.checks.failed ? `, ${pr.checks.failed} failing` : ''}` : ''
  return `PR #${pr.number} · ${pr.draft && pr.state === 'open' ? 'draft' : pr.state}${checks}`
}
const REVIEW_TEXT = { approved: 'approved', changes_requested: 'changes requested', review_required: 'review required' }
// Reasons not worth a line under the branch: no gh, no repository or branch, no pull request.
const QUIET_PR = new Set(['no_gh', 'no_git', 'no_repo', 'no_branch', 'no_remote', 'no_pr'])

// The paths a Stage or Unstage button acts on: a rename stages or unstages both of its sides. A conflict, and a
// folder Git lists whole (a nested repository), have no button: the server stages files only.
const entryPaths = entry => entry.orig_path ? [entry.path, entry.orig_path] : [entry.path]
// A submodule whose commit changed is listed, but the server stages files only.
const stageable = entry => !entry.hidden && !entry.submodule && entry.status !== 'conflicted' && !entry.path.endsWith('/')

// Git's view of the project (GET /git/status), grouped like an editor's source control
// view, and a read-only diff of one file (GET /git/diff). Refreshes when shown, on
// Refresh, and when a response ends. onOpenFile(path, line) opens the file in the editor.
// Stage and Unstage (POST /git/stage) act on the files you choose; Commit (POST /git/commit)
// commits what is staged with your message; the pull request chip comes from gh (GET /git/pr).
function GitChanges({ query, workspace, busy, onOpenFile }) {
  const [status, setStatus] = useState(null)
  const [selected, setSelected] = useState(null)  // { entry, diff?, error? }
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [acting, setActing] = useState(false)  // a stage, unstage or commit request is running
  const [message, setMessage] = useState('')
  const [committed, setCommitted] = useState(null)  // { hash, subject } of the last commit made here
  const [pr, setPr] = useState(null)
  const latest = useRef(0)
  const prRequest = useRef(0)
  const ids = useRef(`git-${Math.random().toString(36).slice(2, 10)}`).current  // Stage buttons name their row's path.
  const withQuery = (path, extra = '') => `${path}${query || extra ? `?${[query, extra].filter(Boolean).join('&')}` : ''}`
  const loadPr = async (result, force = false) => {
    const request = ++prRequest.current
    if (!result?.repository || !result.branch) { setPr(null); return }
    try {
      const answer = await api(withQuery('/git/pr', `branch=${encodeURIComponent(result.branch)}${force ? '&refresh=true' : ''}`))
      if (request === prRequest.current) setPr(answer)
    } catch { if (request === prRequest.current) setPr(null) }  // The chip is optional: no error for it.
  }
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
  const refresh = useCallback(async (force = false) => {
    const request = ++latest.current
    setLoading(true); setError('')
    try {
      const result = await api(`/git/status?${query}`)
      if (request === latest.current) { setStatus(result); void loadPr(result, force) }
    } catch (e) { if (request === latest.current) setError(e.message) }
    finally { if (request === latest.current) setLoading(false) }
  }, [query])
  useEffect(() => { void refresh() }, [refresh])
  const wasBusy = useRef(busy)
  useEffect(() => { if (wasBusy.current && !busy) void refresh(); wasBusy.current = busy }, [busy])
  // Stage, unstage and commit answer with the new status; a newer refresh still wins.
  const act = async (path, body) => {
    const request = ++latest.current
    setActing(true); setError('')
    try {
      const result = await api(withQuery(path), 'POST', body)
      const next = result.status || result
      if (request === latest.current) setStatus(next)
      return result
    } catch (e) { if (request === latest.current) setError(e.message); return null }
    finally { setActing(false); if (request === latest.current) setLoading(false) }
  }
  const stageFiles = (paths, staged) => { setCommitted(null); return act('/git/stage', { paths, staged }) }
  const commit = async event => {
    event.preventDefault()
    const result = await act('/git/commit', { message })
    if (result?.commit) { setCommitted(result.commit); setMessage(''); void loadPr(result.status) }
  }
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
  const stagedCount = files.filter(entry => entry.area === 'staged').length
  // While the agent works it may be changing the same files and index: the server refuses then, and so do the buttons.
  const locked = acting || busy
  const lockedTitle = busy ? 'Wait for the reply to finish' : undefined
  return html`<div class="changes-view">
    <div class="file-path"><${GitBranch} size=${14} /><span title=${status?.branch || ''}>${status?.repository === false ? 'Working tree' : status?.branch || (status ? 'Detached HEAD' : 'Working tree')}</span>
      <button class="icon-button" aria-label="Refresh changes" disabled=${loading} onClick=${() => void refresh(true)}><${RefreshCw} size=${15} className=${loading ? 'spin' : ''} /></button></div>
    ${pr?.available ? html`<p class="git-pr"><a class=${`git-pr-chip ${pr.draft && pr.state === 'open' ? 'draft' : pr.state}${pr.checks?.failed ? ' failing' : ''}`}
        href=${pr.url || undefined} target="_blank" rel="noopener noreferrer"
        title=${[pr.title, REVIEW_TEXT[pr.review_decision]].filter(Boolean).join(' · ')}>${prChipText(pr)}</a></p>`
      : pr && !QUIET_PR.has(pr.code) ? html`<p class="git-pr git-pr-note" title=${pr.reason}>${pr.reason}</p>` : null}
    ${error ? html`<div class="panel-error" role="alert">${error}</div>` : null}
    ${committed ? html`<p class="git-committed" role="status">Committed ${committed.hash}: ${committed.subject}</p>` : null}
    <div class="git-groups" ref=${list}>
      ${status?.repository === false ? html`<p class="panel-empty">Not a Git repository.</p>`
        : status && !files.length ? html`<p class="panel-empty">Your working tree is clean.</p>`
        : GIT_GROUPS.map(([area, label]) => {
          const group = files.filter(entry => entry.area === area)
          const staged = area === 'staged'
          const usable = group.filter(stageable)
          const verb = staged ? 'Unstage' : 'Stage'
          return group.length ? html`<section key=${area} class="git-group" aria-label=${label}>
            <div class="git-group-head"><h3>${label} (${group.length})</h3>
              ${usable.length > 1 ? html`<button type="button" class="git-group-action" disabled=${locked} title=${lockedTitle}
                onClick=${() => void stageFiles(usable.flatMap(entryPaths), !staged)}>${verb} all</button>` : null}</div>
            ${staged ? html`<form class="git-commit" onSubmit=${commit}>
              <textarea aria-label="Commit message" rows=${2} maxLength=${10000} spellcheck=${true} value=${message}
                placeholder="Commit message" disabled=${acting} onInput=${e => setMessage(e.currentTarget.value)} />
              <button type="submit" disabled=${locked || !message.trim()} title=${lockedTitle}>Commit ${stagedCount === 1 ? '1 file' : `${stagedCount} files`}</button>
            </form>` : null}
            ${group.map((entry, index) => {
              const [letter, name] = GIT_STATUS[entry.status] || ['?', entry.status]
              const pathId = `${ids}-${area}-${index}`
              const content = html`<span class=${`git-letter ${entry.status}`} title=${name} aria-label=${name}>${letter}</span>
                <span class="git-path" id=${pathId} title=${entry.orig_path ? `${entry.orig_path} → ${entry.path}` : entry.path}>${entry.path}</span>`
              return entry.hidden ? html`<div key=${entry.path} class="git-row hidden" title=${HIDDEN_NOTES[entry.hidden]}>${content}<span class="git-hidden">${HIDDEN_NOTES[entry.hidden]}</span></div>`
                : html`<div key=${entry.path} class="git-entry"><button class="git-row" data-area=${entry.area} data-path=${entry.path} onClick=${() => void showDiff(entry)}>${content}
                  ${Number.isInteger(entry.added) && Number.isInteger(entry.removed) ? html`<${DiffStat} added=${entry.added} removed=${entry.removed} />` : null}</button>
                  ${!stageable(entry) ? null : html`<button type="button" class="icon-button git-stage" aria-label=${verb} aria-describedby=${pathId}
                    data-path=${entry.path} title=${lockedTitle || `${verb} ${entry.path}`} disabled=${locked}
                    onClick=${() => void stageFiles(entryPaths(entry), !staged)}>${staged ? html`<${Minus} size=${14} />` : html`<${Plus} size=${14} />`}</button>`}</div>`
            })}</section>` : null
        })}
      ${status?.truncated ? html`<p class="panel-empty">Git lists more files than shown here.</p>` : null}
    </div>
    <p class="panel-footnote">Git's view of the project, including changes made by commands. Credential files are listed without their contents.</p>
  </div>`
}

// The panel's width, set by dragging or with the keyboard on its left edge, and kept for this browser.
// Phone screens (the panel covers the window) keep the stylesheet's width.
const PANEL_WIDTH_KEY = 'workspace-panel-width'
const PANEL_MIN = 320
const PANEL_STEP = 32
function savedWidth() {
  try {
    const value = Number(localStorage.getItem(PANEL_WIDTH_KEY))
    return Number.isFinite(value) && value >= PANEL_MIN ? value : null
  } catch { return null }
}
function saveWidth(value) {
  try { if (value === null) localStorage.removeItem(PANEL_WIDTH_KEY); else localStorage.setItem(PANEL_WIDTH_KEY, String(Math.round(value))) } catch {}
}
// The widest the panel may be: docked, it leaves the chat column at least 420 px; over the chat (601 to
// 1,023 px), it stops at the sidebar like the stylesheet's drawer (8 px from it).
function widest() {
  const docked = window.innerWidth >= 1024
  const sidebar = window.innerWidth > 600 ? (document.querySelector('.app-shell > .sidebar')?.getBoundingClientRect().width || 0) : 0
  return Math.max(PANEL_MIN, Math.floor(window.innerWidth - sidebar - (docked ? 420 : 8)))
}
export function usePanelWidth() {
  const [width, setWidth] = useState(savedWidth)
  const [limit, setLimit] = useState(() => widest())
  useEffect(() => {
    const resize = () => setLimit(widest())
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [])
  // keep false while dragging: the width is saved once, when the drag ends.
  const set = (value, keep = true) => {
    const next = value === null ? null : Math.min(Math.max(Math.round(value), PANEL_MIN), widest())
    setWidth(next)
    if (keep) saveWidth(next)
  }
  return { width: width === null ? null : Math.min(width, limit), limit, set }
}

// The tab the panel shows, kept for this browser: the panel opens again (in any chat) on the tab last chosen.
const PANEL_TAB_KEY = 'workspace-panel-tab'
const PANEL_TABS = ['files', 'changes', 'terminal', 'progress']
function savedTab() {
  try {
    const value = localStorage.getItem(PANEL_TAB_KEY)
    return PANEL_TABS.includes(value) ? value : 'files'
  } catch { return 'files' }
}
function saveTab(value) {
  try { localStorage.setItem(PANEL_TAB_KEY, value) } catch {}
}

// The tabs show their icons only while their labels do not fit the panel (a narrow panel, or large text); each
// label stays its button's name and tooltip. Measured again when the tabs, a tab or the text size changes.
function useFittedTabs() {
  const nav = useRef(null)
  useLayoutEffect(() => {
    const element = nav.current
    if (!element || typeof ResizeObserver !== 'function') return undefined
    const fit = () => {
      element.classList.remove('icons-only')
      element.classList.toggle('icons-only', element.scrollWidth > element.clientWidth + 1)
    }
    fit()
    const sizes = new ResizeObserver(fit)
    sizes.observe(element)
    element.querySelectorAll('button').forEach(button => sizes.observe(button))
    const scale = new MutationObserver(fit)  // Settings → Appearance → Text size (--text-scale on the root).
    scale.observe(document.documentElement, { attributes: true, attributeFilter: ['style'] })
    void document.fonts?.ready.then(() => { if (element.isConnected) fit() })
    return () => { sizes.disconnect(); scale.disconnect() }
  }, [])
  return nav
}

function PanelResizer({ panel, width, limit, onWidth }) {
  const drag = useRef(null)
  const current = () => width ?? panel.current?.getBoundingClientRect().width ?? PANEL_MIN
  const onPointerDown = event => {
    if (event.button !== 0) return
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = { x: event.clientX, width: current() }
    document.body.classList.add('panel-resizing')
  }
  const onPointerMove = event => {
    if (!drag.current) return
    drag.current.moved = drag.current.width + drag.current.x - event.clientX  // Dragging left widens the panel.
    onWidth(drag.current.moved, false)
  }
  const end = () => {
    if (drag.current?.moved !== undefined) onWidth(drag.current.moved)
    drag.current = null
    document.body.classList.remove('panel-resizing')
  }
  const onKeyDown = event => {
    const step = event.shiftKey ? PANEL_STEP * 4 : PANEL_STEP
    const next = { ArrowLeft: current() + step, ArrowRight: current() - step, Home: PANEL_MIN, End: limit }[event.key]
    if (next !== undefined) { event.preventDefault(); onWidth(next) }
    else if (event.key === 'Enter') { event.preventDefault(); onWidth(null) }
  }
  useEffect(() => end, [])
  return html`<div class="panel-resizer" role="separator" aria-orientation="vertical" tabindex="0"
    aria-label="Resize workspace panel" aria-valuemin=${PANEL_MIN} aria-valuemax=${limit} aria-valuenow=${Math.round(current())}
    title="Drag to resize. Arrow keys resize, Enter or a double-click restores the default width."
    onPointerDown=${onPointerDown} onPointerMove=${onPointerMove} onPointerUp=${end} onPointerCancel=${end} onLostPointerCapture=${end}
    onDblClick=${() => onWidth(null)} onKeyDown=${onKeyDown} />`
}

// file: { id, path, content, original, mode?: 'edit'|'review'|'preview', binary?, note?, version? } | null; setFile
// accepts a value or an updater. A binary file (an image, a PDF, or a file that is not text: note says why) has no
// text and only a preview or Download; Markdown and HTML files switch between the editor and a preview.
// changes: the conversation's unreviewed agent changes (see ChangesBar); review: { path } opens
// that file's changes once, then onReviewOpened() clears it; onReview(action, files) keeps or undoes;
// onSelectSession(id) opens a chat that a review error names as blocking.
// onSendComments(message) sends the review's line comments to the agent (see DiffReview).
// allowedDirectories: the conversation's granted folders (absolute), offered as other roots of Files.
// codingDir: Settings coding_dir_status (the default code folder, a root of Files in every chat);
// pythonVenv: Settings python_venv_status (named in the Terminal).
// open: { path, line? } opens that file in the editor once, at that line, then onOpened() clears it.
// worktree: the chat's worktree ({ branch, source_workspace }) or null; the header then names the project and branch.
// Progress (ProgressPanel): session (the open chat or null), sessions (app.sessions), tasks (the chat's task list);
// onOpenSubagent(id) opens a subagent chat, onShowCard(eventId) shows a card in the transcript and onOpenContext()
// the context meter's details. showTab: a tab name to show once, then onTabShown() clears it (Show in panel).
export default function WorkspacePanel({ sessionId, workspace, worktree = null, allowedDirectories = [], codingDir = null, pythonVenv = null, file, setFile, onClose, onAttach, changes: agentChanges, busy, review, onReviewOpened, onReview, onSelectSession, onSendComments, open, onOpened,
  session = null, sessions = [], tasks = [], onOpenSubagent, onShowCard, onOpenContext, showTab = null, onTabShown }) {
  const [tab, shownTab] = useState(savedTab)
  const setTab = name => { shownTab(name); saveTab(name) }
  // Progress → View output: the job Terminal selects when it opens.
  const [focusJob, setFocusJob] = useState(null)
  const tabs = useFittedTabs()
  const panel = useRef(null)
  const sizing = usePanelWidth()
  // The drawer's error alert (outside the panel) is as wide as the panel.
  useEffect(() => {
    const shell = panel.current?.closest('.app-shell')
    if (!shell) return undefined
    if (sizing.width === null) shell.style.removeProperty('--panel-width')
    else shell.style.setProperty('--panel-width', `${sizing.width}px`)
    return () => shell.style.removeProperty('--panel-width')
  }, [sizing.width])
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
  const rowIds = useRef(`files-${Math.random().toString(36).slice(2, 10)}`).current  // Download buttons name their row's file.
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
  // A path as a reply, a tool card or Git named it: relative to the project when inside it, without './'.
  const normalize = requested => {
    let value = String(requested || '').replace(/^(?:\.\/)+/, '')
    const base = workspace.replace(/\/+$/, '')
    if (base && value.startsWith(`${base}/`)) value = value.slice(base.length + 1)
    return value || requested
  }
  const openFile = async (requested, chosen, line) => {
    const path = normalize(requested)
    const mode = chosen || (changed.has(path) ? 'review' : 'edit')
    if (file?.path === path && mode === 'review') { setFile({ ...file, mode }); return }  // Keeps an unsaved draft.
    if (dirty && !confirm('Discard your unsaved edits?')) return
    const request = ++openRequest.current
    setError('')
    // Images and PDF open in the preview, from their bytes; an agent's change to one opens its review.
    if (mode !== 'review' && opensInPreview(path)) {
      pendingLine.current = null
      setLineMark(null)
      setFile({ id: crypto.randomUUID(), path, content: '', original: '', mode: 'preview', binary: true })
      return
    }
    try {
      const result = await api(`/file?path=${encodeURIComponent(path)}&${query}`)
      if (request !== openRequest.current) return
      // The server can find a file named from another folder (the project's own folder name first,
      // or a worktree's source project); the editor then works on the file it found.
      const opened = typeof result.path === 'string' && result.path ? result.path : path
      const id = crypto.randomUUID()
      pendingLine.current = line ? { id, line } : null
      setLineMark(null)
      setFile({ id, path: opened, content: result.content, original: result.content, absolute: result.absolute,
        mode: mode === 'review' && !changed.has(opened) ? 'edit' : mode, ...(opened !== path ? { requested: path } : {}) })
    } catch (e) {
      if (request !== openRequest.current) return
      // A file the agent deleted, or that a command made unreadable, still has a review.
      if (mode === 'review' || changed.has(path)) setFile({ id: crypto.randomUUID(), path, content: '', original: '', mode: 'review', missing: true })
      // A file that is not text (binary, not UTF-8 or over the editor's limit) can still be downloaded. The server
      // names the file it opened, as for a text file.
      else if (e.detail?.code === 'not_text') {
        const opened = typeof e.detail.path === 'string' && e.detail.path ? e.detail.path : path
        setFile({ id: crypto.randomUUID(), path: opened, content: '', original: '', mode: 'preview', binary: true, note: e.message,
          ...(typeof e.detail.absolute === 'string' ? { absolute: e.detail.absolute } : {}), ...(opened !== path ? { requested: path } : {}) })
      }
      else setError(e.message)
    }
  }
  const download = async path => {
    setError('')
    try { await downloadFile(path, query) } catch (e) { setError(e.message) }
  }

  // Show the text editor (or a Markdown or HTML file's preview) again with the file as it is on disk, keeping an
  // unsaved draft. An image or PDF shows its preview again, read anew.
  const editFile = async (quiet = false, mode = 'edit') => {
    if (!file) return
    if (dirty) { setFile({ ...file, mode }); return }
    if (file.missing && quiet) { setFile(null); return }
    if (file.binary || opensInPreview(file.path)) {
      setFile(current => current?.id === file.id ? { ...current, mode: 'preview', binary: true, missing: false, version: (current.version || 0) + 1 } : current)
      return
    }
    const request = ++openRequest.current
    try {
      const result = await api(`/file?path=${encodeURIComponent(file.path)}&${query}`)
      if (request === openRequest.current) setFile(current => current?.id === file.id ? { ...current, content: result.content, original: result.content, mode, missing: false, absolute: result.absolute } : current)
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
  // A run's Show in panel (and any other one-shot request for a tab).
  useEffect(() => {
    if (!showTab) return
    onTabShown?.()
    if (PANEL_TABS.includes(showTab)) setTab(showTab)
  }, [showTab])
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
  // The agent changed the open file again after Keep or Undo: its review opens again. An editor without
  // unsaved edits follows the agent's later edits. Only for the same file, once the changes list is known.
  const pendingHash = pending?.current_hash || null
  const lastPending = useRef({ path: file?.path, hash: pendingHash, known: !!agentChanges })
  useEffect(() => {
    const before = lastPending.current
    lastPending.current = { path: file?.path, hash: pendingHash, known: !!agentChanges }
    if (!file || dirty || !before.known || before.path !== file.path || before.hash === pendingHash || !pendingHash) return
    if (!before.hash && file.mode !== 'review') setFile({ ...file, mode: 'review' })
    else if (file.mode === 'edit' || file.mode === 'preview') void editFile(true, file.mode)
  }, [file?.path, pendingHash, !!agentChanges])
  // New or removed changed files can add or remove folders and files: list the folder again.
  const changedKey = [...changed.keys()].sort().join('\n')
  const listedKey = useRef(changedKey)
  useEffect(() => {
    if (listedKey.current === changedKey) return
    listedKey.current = changedKey
    if (tab === 'files' && !file) void refresh()
  }, [changedKey])
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
  // While the labels do not fit, the tabs show their icons only (useFittedTabs).
  const tabButton = (name, Icon, label) => html`<button class=${tab === name ? 'active' : ''} aria-pressed=${tab === name} title=${label}
    onClick=${() => setTab(name)}><${Icon} size=${15} /><span class="tab-label">${label}</span></button>`
  const branch = typeof worktree?.branch === 'string' ? worktree.branch : ''
  const project = (branch && typeof worktree.source_workspace === 'string' ? worktree.source_workspace : workspace)
    .split('/').filter(Boolean).pop() || workspace
  const expanded = sizing.width !== null && sizing.width >= sizing.limit
  const kind = file ? previewKind(file.path) : null
  // An image other than SVG reaches the agent only attached to a message (read_file reads text): no Add to chat.
  const raster = kind === 'image' && !/\.svg$/i.test(file.path)
  // The server read an image or PDF by another path (a worktree's own copy, or a name from another folder): the
  // panel names that file, as it does a text file the server found; the preview keeps reading the path it was given.
  const previewOpened = opened => setFile(current => current?.id === file?.id && opened !== current.path
    ? { ...current, path: opened, requested: current.requested || current.path } : current)
  // Markdown and HTML files switch between the editor and their preview (of the editor's text, unsaved edits included).
  const textPreview = !!file && !file.binary && !file.missing && (kind === 'markdown' || kind === 'html')
  const previewing = !!file && file.mode === 'preview' && !reviewing
  const absolute = file?.absolute || (file?.binary ? (file.path.startsWith('/') ? file.path : `${workspace.replace(/\/+$/, '')}/${file.path}`) : null)
  const openedNote = file?.requested ? html`<p class="editor-note" role="status">Opened ${file.path}: the reply named it ${file.requested}.</p>` : null
  return html`<aside class=${`workspace-panel ${sizing.width !== null ? 'sized' : ''}`} aria-label="Workspace panel" ref=${panel}
    style=${sizing.width !== null ? { '--panel-width': `${sizing.width}px` } : null}>
    <${PanelResizer} panel=${panel} width=${sizing.width} limit=${sizing.limit} onWidth=${sizing.set} />
    <header><div><h2>Workspace</h2><p class="workspace-project" title=${workspace}><span>${project}</span>
        ${branch ? html`<span class="branch-badge" title=${`Worktree branch ${branch}`}><${GitBranch} size=${11} /><span>${branch}</span></span>` : null}</p></div>
      <div class="workspace-header-actions">
        <button class="icon-button panel-expand" aria-label=${expanded ? 'Restore workspace width' : 'Expand workspace'} title=${expanded ? 'Restore the default width' : 'Make the panel as wide as possible'}
          onClick=${() => sizing.set(expanded ? null : sizing.limit)}>${expanded ? html`<${Minimize2} size=${16} />` : html`<${Maximize2} size=${16} />`}</button>
        <button class="icon-button" aria-label="Close workspace" onClick=${onClose}><${X} size=${19} /></button></div></header>
    <nav class="workspace-tabs" aria-label="Workspace views" ref=${tabs}>
      ${tabButton('files', Folder, 'Files')}${tabButton('changes', GitBranch, 'Changes')}${tabButton('terminal', Terminal, 'Terminal')}${tabButton('progress', Activity, 'Progress')}
    </nav>
    ${error ? html`<div class="panel-error" role="alert">${error}</div>` : null}
    ${tab === 'files' ? (file ? html`<div class="file-editor">
        <div class="editor-toolbar"><button class="icon-button" aria-label="Back to files" onClick=${() => { if (!dirty || confirm('Discard unsaved edits?')) setFile(null) }}><${ArrowLeft} size=${16} /></button>
          <span title=${file.path}>${file.path}${dirty ? ' •' : ''}</span>
          ${reviewing ? (file.missing ? null : html`<button class="editor-mode" onClick=${() => void editFile()}><${Pencil} size=${14} />Edit</button>`)
            : pending ? html`<button class="editor-mode pending" onClick=${() => setFile({ ...file, mode: 'review' })}><${FileDiff} size=${14} />Review changes</button>` : null}
          ${textPreview && !reviewing ? (previewing
            ? html`<button class="editor-mode" onClick=${() => setFile({ ...file, mode: 'edit' })}><${Pencil} size=${14} />Edit</button>`
            : html`<button class="editor-mode" onClick=${() => setFile({ ...file, mode: 'preview' })}><${Eye} size=${14} />Preview</button>`) : null}
          <${VSCodeLink} absolute=${absolute} line=${markedLine?.line} />
          ${raster ? null : html`<button class="icon-button" onClick=${() => onAttach(file.path)} aria-label="Add this file to chat"><${Plus} size=${16} /></button>`}
          ${file.missing ? null : html`<button class="icon-button" onClick=${() => void download(file.path)} aria-label="Download file" title="Download"><${Download} size=${16} /></button>`}
          ${file.binary ? null : html`<button class="icon-button" onClick=${() => void save()} disabled=${!dirty || loading || reviewing} aria-label="Save file"><${Save} size=${16} /></button>`}
        </div>
        ${reviewing ? html`<${DiffReview} key=${file.path} sessionId=${sessionId} path=${file.path} version=${pending.current_hash} busy=${busy} onAction=${onReview} onSelectSession=${onSelectSession}
          onSendComments=${onSendComments} />` : previewing ? (file.binary && !opensInPreview(file.path)
            ? html`<div class="file-preview-none"><p>${file.note || 'This file cannot be shown here.'}</p>
              <button type="button" class="outline" onClick=${() => void download(file.path)}><${Download} size=${14} />Download</button></div>
              ${openedNote}`
            : html`<${FilePreview} key=${file.id} path=${file.path} source=${file.binary ? file.requested || file.path : undefined} kind=${kind}
              content=${file.content} query=${query} version=${file.version || 0} onOpenPath=${path => void openFile(path)} onOpened=${previewOpened} />
              ${openedNote}
              ${textPreview ? html`<div class="editor-status"><span>Preview</span><span>${dirty ? 'Unsaved changes' : 'Saved on disk'}</span></div>` : null}`) : html`
        <textarea ref=${editor} class="code-editor" aria-label=${`Edit ${file.path}`} value=${file.content} spellcheck=${false}
          onMouseDown=${leaveLine} onKeyDown=${leaveLine} onInput=${e => { leaveLine(); setFile({ ...file, content: e.currentTarget.value }) }} />
        ${openedNote}
        <div class="editor-status"><span>${file.content.split('\n').length} lines${markedLine?.selected ? ` · Line ${markedLine.line}` : ''}</span><span>${dirty ? 'Unsaved changes' : 'Saved on disk'}</span></div>`}
      </div>` : html`<div class="file-browser">
        <${FileRoots} workspace=${workspace} root=${root} onChoose=${chooseRoot} granted=${allowedDirectories} folders=${added.folders} codingDir=${codingDir}
          onAdd=${added.add} onRemove=${added.remove} changed=${[...changed.keys()]} />
        <div class="file-path"><button class="icon-button" aria-label="Parent folder" disabled=${directory === root} onClick=${() => setDirectory(parentOf(directory))}><${ArrowLeft} size=${15} /></button><span title=${directory === '.' ? workspace : directory}>${directory === '.' ? 'Project files' : directory}</span>
          <button class="icon-button" aria-label="Refresh files" disabled=${loading} onClick=${() => void refresh()}><${RefreshCw} size=${14} className=${loading ? 'spin' : ''} /></button></div>
        <div class="file-list">${files.map((entry, index) => {
          // Like an editor's explorer: a dot on files the agent changed, and on folders containing them.
          const dot = entry.directory ? [...changed.keys()].some(path => path.startsWith(`${entry.path}/`)) : changed.has(entry.path)
          return html`<div key=${entry.path} class="file-entry"><button class="file-row" onClick=${() => entry.directory ? setDirectory(entry.path) : void openFile(entry.path)}>
          ${entry.directory ? html`<${Folder} size=${16} />` : html`<${File} size=${16} />`}<span id=${`${rowIds}-${index}`} title=${entry.name}>${entry.name}</span>
          ${dot ? html`<span class="change-dot" role="img" aria-label=${entry.directory ? 'Contains changed files' : 'Changed by the agent'} title=${entry.directory ? 'Contains files the agent changed' : 'Changed by the agent: open it to review'} />` : null}
          ${entry.directory ? html`<${ChevronRight} size=${13} />` : null}
        </button>${entry.directory ? null : html`<button type="button" class="icon-button file-download" aria-label="Download"
          aria-describedby=${`${rowIds}-${index}`} title=${`Download ${entry.name}`} onClick=${() => void download(entry.path)}><${Download} size=${14} /></button>`}</div>`
        })}${!loading && files.length === 0 ? html`<p class="panel-empty">This folder is empty.</p>` : null}</div>
        <p class="panel-footnote">Secret files and generated folders are excluded.</p>
      </div>`)
    : tab === 'changes' ? html`<${GitChanges} query=${query} workspace=${workspace} busy=${busy}
      onOpenFile=${(path, line) => { setTab('files'); void openFile(path, 'edit', line) }} />`
    : tab === 'progress' ? html`<${ProgressPanel} session=${session} sessions=${sessions} tasks=${tasks} onSelectSession=${onOpenSubagent}
      onShowCard=${onShowCard} onOpenContext=${onOpenContext} onError=${setError}
      onViewOutput=${id => { setFocusJob(id); setTab('terminal') }} />`
    : html`<${TerminalPanel} sessionId=${sessionId} workspace=${workspace} pythonVenv=${pythonVenv} focusJob=${focusJob} onFocused=${() => setFocusJob(null)} />`}
    ${sessionId && !(tab === 'files' && reviewing) ? html`<${UnsentComments} sessionId=${sessionId} busy=${busy} onSendComments=${onSendComments} />` : null}
  </aside>`
}
