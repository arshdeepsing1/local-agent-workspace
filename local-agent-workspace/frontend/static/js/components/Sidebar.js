import { html, useAutoFocus, useEffect, useLayoutEffect, useMemo, useRef, useState } from '../lib.js'
import {
  ArrowLeftRight, Bell, BellOff, ChevronDown, ChevronRight, Code2, Ellipsis, Folder, GitBranch, Pin, Plus,
  Search, Settings as SettingsIcon, Tag, X,
} from '../icons.js'
import DeleteConversationDialog from './DeleteConversationDialog.js'

export function Brand({ small = false }) {
  return html`<span class=${`brand-symbol ${small ? 'small' : ''}`} aria-hidden="true"><i /><i /><i /><i /></span>`
}

const ROWS_PER_GROUP = 50
const SEARCH_DELAY_MS = 400
const SEARCH_MIN_CHARS = 3
const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`
const fold = text => String(text || '').toLowerCase()
// A worktree chat belongs to the project it was made from.
export const projectKey = session => session.worktree?.source_workspace || session.workspace || ''
const folderName = path => path.split('/').filter(Boolean).pop() || path || 'No project'
// Only a list of strings counts as tags, so a damaged header cannot break the list.
const tagsOf = session => Array.isArray(session.tags) ? session.tags.filter(tag => typeof tag === 'string') : []
const MAX_TAGS = 10
// '#word' in the search box filters by tag (a tag starting with word); the other words search titles and messages.
const hashTerm = word => word.length > 1 && word.startsWith('#')

// Subagent chats nest under their parent when it is listed and still linked (one level, so a
// damaged parent cycle cannot hide both chats). Archived and pinned chats are never nested.
function organize(sessions) {
  const byId = new Map(sessions.map(session => [session.id, session]))
  const childCount = new Map()
  const nestable = session => !!session.parent_session_id && session.parent_session_id !== session.id
    && byId.has(session.parent_session_id) && !session.parent_deleted && !session.archived && !session.pinned
  const children = new Map()
  const topLevel = []
  for (const session of sessions) {
    const parent = session.parent_session_id
    if (parent) childCount.set(parent, (childCount.get(parent) || 0) + 1)
    if (nestable(session) && !nestable(byId.get(parent))) {
      if (!children.has(parent)) children.set(parent, [])
      children.get(parent).push(session)
    } else topLevel.push(session)
  }
  return { children, childCount, topLevel }
}

// Pinned (across projects), one group per project with the current one first, and Archived.
// tags: chosen in the Tags menu (a chat with any of them passes); hashes: '#word' search terms (every one must match a tag).
function arrange(sessions, tree, { runningOnly, terms, tags, hashes, current }) {
  const filtering = runningOnly || terms.length > 0 || tags.length > 0 || hashes.length > 0
  const chosen = tags.map(fold)
  const passes = session => {
    const own = tagsOf(session).map(fold)
    return (!runningOnly || session.status !== 'idle')
      && (!chosen.length || chosen.some(item => own.includes(item)))
      && hashes.every(hash => own.some(item => item.startsWith(hash)))
      && terms.every(term => fold(session.title).includes(term))
  }
  // While filtering, matching subagent chats are listed on their own.
  const pool = filtering ? sessions.filter(passes) : tree.topLevel
  // The chats a tag filter allows, for the message search results (null: no tag filter).
  const tagged = chosen.length || hashes.length ? new Set(sessions.filter(session => {
    const own = tagsOf(session).map(fold)
    return (!chosen.length || chosen.some(item => own.includes(item))) && hashes.every(hash => own.some(item => item.startsWith(hash)))
  }).map(session => session.id)) : null
  const pinned = []
  const archived = []
  const groups = new Map()
  for (const session of pool) {
    if (session.archived) archived.push(session)  // Archived wins over pinned.
    else if (session.pinned) pinned.push(session)
    else {
      const key = projectKey(session)
      if (!groups.has(key)) groups.set(key, [])
      groups.get(key).push(session)
    }
  }
  // Groups keep the order of their newest chat, with the current project first.
  const ordered = [...groups.entries()].sort(([a], [b]) => (b === current) - (a === current))
  return { filtering, pinned, archived, groups: ordered, listed: pool.length - archived.length, tagged }
}

function RenameForm({ session, title, setTitle, saving, error, onSave, onCancel }) {
  const input = useAutoFocus()
  return html`<form class="conversation-rename" aria-label=${`Rename ${session.title}`} aria-busy=${saving}
    onSubmit=${event => { event.preventDefault(); onSave() }}
    onKeyDown=${event => { if (event.key === 'Escape') { event.preventDefault(); if (!saving) onCancel() } }}>
    <label for=${`rename-${session.id}`}>Conversation name</label>
    <input ref=${input} id=${`rename-${session.id}`} required maxLength=${160} value=${title} disabled=${saving}
      aria-invalid=${!!error} aria-describedby=${error ? `rename-error-${session.id}` : undefined}
      onFocus=${event => event.currentTarget.select()} onInput=${event => setTitle(event.currentTarget.value)} />
    <div><button type="submit" class="primary" disabled=${saving || !title.trim()}>${saving ? 'Saving…' : 'Save name'}</button>
      <button type="button" class="outline" disabled=${saving} onClick=${onCancel}>Cancel rename</button></div>
    ${error ? html`<p id=${`rename-error-${session.id}`} class="form-error" role="alert">${error}</p>` : null}
  </form>`
}

function TagsForm({ session, onSave, onCancel }) {
  const input = useAutoFocus()
  const [text, setText] = useState(tagsOf(session).join(', '))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  // After a refused save the input is enabled again: keep typing (and Esc) there.
  useEffect(() => { if (error && !saving) input.current?.focus() }, [error, saving])
  const save = async () => {
    if (saving) return
    // Tags that differ only in case count as one, as on the server.
    const seen = new Set()
    const tags = text.split(',').map(tag => tag.trim().replace(/\s+/g, ' ')).filter(tag => {
      const key = tag.normalize('NFC').toLowerCase()
      if (!tag || seen.has(key)) return false
      seen.add(key)
      return true
    })
    if (tags.length > MAX_TAGS) { setError(`Use at most ${MAX_TAGS} tags.`); return }
    setSaving(true); setError('')
    try { await onSave(tags); onCancel() }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not save the tags.'); setSaving(false) }
  }
  return html`<form class="conversation-rename" aria-label=${`Edit tags of ${session.title}`} aria-busy=${saving}
    onSubmit=${event => { event.preventDefault(); void save() }}
    onKeyDown=${event => { if (event.key === 'Escape') { event.preventDefault(); if (!saving) onCancel() } }}>
    <label for=${`tags-${session.id}`}>Tags (comma-separated)</label>
    <input ref=${input} id=${`tags-${session.id}`} value=${text} disabled=${saving} spellcheck=${false}
      aria-invalid=${!!error} aria-describedby=${error ? `tags-error-${session.id}` : undefined}
      onInput=${event => { setText(event.currentTarget.value); setError('') }} />
    <div><button type="submit" class="primary" disabled=${saving}>${saving ? 'Saving…' : 'Save tags'}</button>
      <button type="button" class="outline" disabled=${saving} onClick=${onCancel}>Cancel</button></div>
    ${error ? html`<p id=${`tags-error-${session.id}`} class="form-error" role="alert">${error}</p>` : null}
  </form>`
}

// Rename, pin, archive, tags and delete of one chat. onMoved(id) runs once a pin or archive change is
// saved, since the row then moves to another section.
function RowMenu({ session, onMeta, onRename, onEditTags, onDelete, onError, onMoved }) {
  const [open, setOpen] = useState(false)
  const root = useRef(null)
  const button = useRef(null)
  const menu = useRef(null)
  // A row near the bottom of the list opens its menu upwards, so every item stays inside the list.
  useLayoutEffect(() => {
    if (!open || !menu.current || !button.current) return
    const list = root.current?.closest('.conversation-list')?.getBoundingClientRect()
    const anchor = button.current.getBoundingClientRect()
    const up = !!list && list.bottom - anchor.bottom < menu.current.offsetHeight + 4 && anchor.top - list.top > list.bottom - anchor.bottom
    menu.current.classList.toggle('up', up)
    menu.current.scrollIntoView({ block: 'nearest' })
  }, [open])
  useEffect(() => {
    if (!open) return
    menu.current?.querySelector('[role="menuitem"]')?.focus({ preventScroll: true })
    const outside = event => { if (!root.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('focusin', outside)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('focusin', outside) }
  }, [open])
  const act = (changes) => {
    setOpen(false)
    button.current?.focus()
    void onMeta(session.id, changes).then(() => onMoved?.(session.id), e => onError?.(e.message))
  }
  return html`<div class="row-menu-anchor" ref=${root} onKeyDown=${event => {
    if (!open) return
    if (event.key === 'Escape') { event.preventDefault(); setOpen(false); button.current?.focus() }
    if (event.key === 'Tab') setOpen(false)
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault()
      const items = Array.from(menu.current?.querySelectorAll('[role="menuitem"]') || [])
      const index = items.indexOf(document.activeElement)
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
      items[next]?.focus()
    }
  }}>
    <button ref=${button} class="row-more" aria-label=${`More actions for ${session.title}`} title="More actions"
      aria-haspopup="menu" aria-expanded=${open} onClick=${() => setOpen(value => !value)}><${Ellipsis} size=${14} /></button>
    ${open ? html`<div class="row-menu" role="menu" aria-label=${`Actions for ${session.title}`} ref=${menu}>
      <button role="menuitem" onClick=${() => { setOpen(false); onRename() }}>Rename</button>
      ${!session.archived ? html`<button role="menuitem" onClick=${() => act({ pinned: !session.pinned })}>${session.pinned ? 'Unpin' : 'Pin'}</button>` : null}
      <button role="menuitem" onClick=${() => act({ archived: !session.archived })}>${session.archived ? 'Unarchive' : 'Archive'}</button>
      <button role="menuitem" onClick=${() => { setOpen(false); onEditTags() }}>Edit tags…</button>
      <button role="menuitem" class="danger" onClick=${() => { setOpen(false); button.current?.focus(); onDelete() }}>Delete…</button>
    </div>` : null}
  </div>`
}

const ACTIVITY = { running: 'Running', compacting: 'Compacting', naming: 'Naming', delegating: 'Delegating' }

// Status after the title, so the row's accessible name still starts with the title.
function RowStatus({ session, unread, failed }) {
  const changed = session.unreviewed_changes > 0 ? html`<span class="changes-dot"
    title=${`${plural(session.unreviewed_changes, 'file')} with unreviewed agent changes`}><span class="sr-only">Unreviewed changes</span></span>` : null
  if (session.status === 'awaiting_approval') return html`${changed}<span class="row-badge approval" title="Waiting for your approval">Needs approval</span>`
  if (session.status !== 'idle') return html`${changed}<span class="sr-only">${ACTIVITY[session.status] || 'Working'}</span>`
  if (failed) return html`${changed}<span class="row-badge error" title="The last reply ended with an error">Error</span>`
  if (unread) return html`${changed}<span class="unread-dot" title="Changed since you last opened it"><span class="sr-only">Unread</span></span>`
  return changed
}

function ConversationRow({ session, selected, unread, failed, showId, childCount, onSelect, onDelete, onAskDelete, onRename, onMeta, onMoved, onTag, onError }) {
  const [editing, setEditing] = useState(null)  // null | 'title' | 'tags'
  const [title, setTitle] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const rowRef = useRef(null)
  const restoreFocus = useRef(false)
  // After a rename or tag edit, focus returns to the row's More actions button.
  useEffect(() => {
    if (!editing && restoreFocus.current) {
      rowRef.current?.querySelector('.row-more')?.focus()
      restoreFocus.current = false
    }
  }, [editing])
  const finish = () => { restoreFocus.current = true; setEditing(null) }
  const save = async () => {
    if (saving || !title.trim()) return
    setSaving(true); setError('')
    try { await onRename(session.id, title); finish() }
    catch (error) { setError(error instanceof Error ? error.message : 'Could not rename this conversation.') }
    finally { setSaving(false) }
  }
  const branch = typeof session.worktree?.branch === 'string' ? session.worktree.branch : ''
  const tags = tagsOf(session)
  return html`<div ref=${rowRef} class=${`conversation-row ${selected ? 'selected' : ''}`} data-session-id=${session.id}>
    ${editing === 'title' ? html`<${RenameForm} session=${session} title=${title} saving=${saving} error=${error}
      setTitle=${value => { setTitle(value); setError('') }} onSave=${() => void save()} onCancel=${finish} />`
    : editing === 'tags' ? html`<${TagsForm} session=${session} onSave=${tags => onMeta(session.id, { tags })} onCancel=${finish} />` : html`
      <button onClick=${onSelect} title=${`${session.title}\nSession: ${session.id}`} aria-current=${selected ? 'page' : undefined}>
        ${session.status !== 'idle' && session.status !== 'awaiting_approval' ? html`<span class="activity-dot" aria-hidden="true" title=${ACTIVITY[session.status] || 'Working'} />` : null}<span class="conversation-name">${session.title}${showId ? html`<small class="session-id"> · ${session.id.slice(0, 8)}</small>` : null}</span>
        <${RowStatus} session=${session} unread=${unread} failed=${failed} />
      </button>
      <${RowMenu} session=${session} onMeta=${onMeta} onError=${onError} onMoved=${onMoved} onEditTags=${() => setEditing('tags')}
        onRename=${() => { setTitle(session.title); setError(''); setEditing('title') }}
        onDelete=${() => { if (childCount > 0) onAskDelete(); else if (confirm('Delete this conversation? Your project files will remain.')) onDelete() }} />`}
  </div>
  ${!editing && (branch || tags.length) ? html`<div class="row-tags">
    ${branch ? html`<span class="branch-badge" title=${`Worktree branch ${branch}`}><${GitBranch} size=${11} /><span>${branch}</span></span>` : null}
    ${tags.map(tag => html`<button key=${tag} class="tag-chip"
      title=${`Show conversations tagged ${tag}`} onClick=${() => onTag(tag)}>${tag}</button>`)}</div>` : null}`
}

// The Tags menu: every tag in use with its number of chats; chats with any checked tag are listed.
function TagFilter({ sessions, selected, onChange }) {
  const [open, setOpen] = useState(false)
  const root = useRef(null)
  const button = useRef(null)
  const counts = useMemo(() => {
    const found = new Map()
    for (const session of sessions) for (const tag of tagsOf(session)) {
      const entry = found.get(fold(tag)) || { tag, count: 0 }
      entry.count++
      found.set(fold(tag), entry)
    }
    return [...found.values()].sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag))
  }, [sessions])
  useEffect(() => {
    if (!open) return
    root.current?.querySelector('input')?.focus({ preventScroll: true })
    const outside = event => { if (!root.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])
  if (!counts.length) return null
  const checked = tag => selected.some(item => fold(item) === fold(tag))
  return html`<div class="tag-menu-anchor" ref=${root} onKeyDown=${event => {
    if (open && event.key === 'Escape') { event.preventDefault(); setOpen(false); button.current?.focus() }
  }}>
    <button ref=${button} class="running-filter tag-filter-toggle" aria-haspopup="true" aria-expanded=${open} aria-pressed=${selected.length > 0}
      title="Filter conversations by tag" onClick=${() => setOpen(value => !value)}><${Tag} size=${11} />Tags${selected.length ? ` (${selected.length})` : ''}</button>
    ${open ? html`<div class="tag-menu" role="group" aria-label="Filter by tag">
      ${counts.map(({ tag, count }) => html`<label key=${fold(tag)}><input type="checkbox" checked=${checked(tag)}
        onChange=${event => onChange(event.currentTarget.checked ? [...selected, tag] : selected.filter(item => fold(item) !== fold(tag)))} />
        <span>${tag}</span><small aria-label=${`${plural(count, 'conversation')}`}>${count}</small></label>`)}
      <p class="tag-menu-hint">Tip: type #tag in the search box.</p>
    </div>` : null}
  </div>`
}

// Turns browser notifications on or off; permission is asked for only by this click.
function NotifyToggle({ notifications }) {
  if (!notifications) return null
  const { enabled, permission, toggle } = notifications
  const hint = permission === 'unsupported' ? 'This browser cannot show notifications.'
    : permission === 'denied' && !enabled ? 'Notifications are blocked for this page in the browser settings.' : ''
  return html`<button class="notify-toggle" role="switch" aria-checked=${enabled} disabled=${!!hint}
    title=${hint || 'Notify me when a conversation needs approval, finishes, or stops with an error'}
    onClick=${() => void toggle()}>${enabled ? html`<${Bell} size=${16} />` : html`<${BellOff} size=${16} />`}Notify me
    <span class="notify-state" aria-hidden="true">${enabled ? 'On' : 'Off'}</span></button>
    ${hint ? html`<p class="notify-hint">${hint}</p>` : null}`
}

function badges(sessions, children) {
  const all = sessions.flatMap(session => [session, ...(children.get(session.id) || [])])
  const waiting = all.filter(session => session.status === 'awaiting_approval').length
  const running = all.filter(session => session.status !== 'idle' && session.status !== 'awaiting_approval').length
  return [waiting ? `${waiting} ${waiting === 1 ? 'needs' : 'need'} approval` : '', running ? `${running} running` : ''].filter(Boolean)
}

// Chats whose messages match, from the server, opened with a click. Title matches are
// left out: the title filter above already lists them.
// tagged: the IDs a tag filter allows (null: no tag filter); other chats' hits are left out too.
const messageHits = (found, tagged = null) => (found?.results || []).filter(result => Array.isArray(result.matches)
  && result.matches.length > 0 && (!tagged || tagged.has(result.id)))
function FoundInMessages({ found, tagged, onOpen }) {
  if (!found) return null
  const hits = messageHits(found, tagged)
  return html`<section class="found-in-messages" aria-label="Found in messages"><p class="section-label">Found in messages</p>
    ${found.error ? html`<p class="empty-sessions" role="alert">${found.error}</p>`
      : hits.length === 0 ? html`<p class="empty-sessions">No messages match.</p>`
      : hits.map(result => html`<button key=${result.id} class="found-result" onClick=${() => onOpen(result.id)}>
        <span class="found-title">${result.title}${result.archived ? html`<small class="muted"> · archived</small>` : null}</span>
        ${result.matches.map(match => html`<small key=${match.event_id} class="found-snippet">${match.snippet}</small>`)}
      </button>`)}
    ${found.truncated ? html`<p class="empty-sessions" role="status">Search stopped early; refine your words.</p>` : null}
  </section>`
}

// Props: settings, connection, sessions, activeId, open, close, onNew, onSelect, onSettings,
// onDelete(id, children?: 'delete') -> Promise, onRename, onMeta(id, { pinned?, archived?, tags? }) -> Promise,
// onSearch(q) -> Promise<{ results, truncated }>, onError(message),
// attention?: { unread(session), failed(session), notifications }
export default function Sidebar(props) {
  const folder = props.settings?.workspace.split('/').filter(Boolean).pop() || 'Choose a project'
  const current = props.settings?.workspace || ''
  const [runningOnly, setRunningOnly] = useState(false)
  const [query, setQuery] = useState('')
  const [tags, setTags] = useState([])  // Chosen in the Tags menu or by clicking a row's tag.
  const [showArchived, setShowArchived] = useState(false)
  const [expanded, setExpanded] = useState({})  // project key -> open
  const [openChildren, setOpenChildren] = useState({})  // parent id -> open
  const [limits, setLimits] = useState({})  // section key -> rows shown
  const [found, setFound] = useState(null)
  const [deleting, setDeleting] = useState(null)  // { session, children }
  const [moved, setMoved] = useState(null)  // id of a chat that Pin or Archive moved
  const aside = useRef(null)
  const searchRequest = useRef(0)
  const words = useMemo(() => fold(query).split(/\s+/).filter(Boolean), [query])
  const terms = useMemo(() => words.filter(word => !hashTerm(word)), [words])
  const hashes = useMemo(() => words.filter(hashTerm).map(word => word.slice(1)), [words])
  const tree = useMemo(() => organize(props.sessions), [props.sessions])
  const view = useMemo(() => arrange(props.sessions, tree, { runningOnly, terms, tags, hashes, current }),
    [props.sessions, tree, runningOnly, terms, tags, hashes, current])
  const addTag = tag => setTags(current => current.some(item => fold(item) === fold(tag)) ? current : [...current, tag])

  // '#tag' words filter the list only; the rest is also searched in messages.
  const text = query.split(/\s+/).filter(word => word && !hashTerm(word)).join(' ').trim()
  // The latest callback, so re-renders (streamed replies, list polls) do not restart a search.
  const onSearch = useRef(props.onSearch)
  onSearch.current = props.onSearch
  useEffect(() => {
    const request = ++searchRequest.current
    if (text.length < SEARCH_MIN_CHARS || !onSearch.current) { setFound(null); return }
    const timer = setTimeout(() => {
      onSearch.current(text).then(result => { if (request === searchRequest.current) setFound(result) },
        error => { if (request === searchRequest.current) setFound({ results: [], truncated: false, error: error.message }) })
    }, SEARCH_DELAY_MS)
    return () => clearTimeout(timer)
  }, [text])

  // The moved row is a new element, so focus follows it to its new place: its More actions
  // button, or when the chat is now hidden, the Archived toggle.
  useEffect(() => {
    if (!moved) return
    setMoved(null)
    if (document.activeElement && document.activeElement !== document.body) return  // Focus is somewhere else now.
    const target = aside.current?.querySelector(`.conversation-row[data-session-id="${CSS.escape(moved)}"] .row-more`)
      || aside.current?.querySelector('.archived-toggle') || aside.current?.querySelector('.conversation-search input')
    target?.focus()
  }, [moved])

  // Titles used by more than one chat: their rows show a short session ID.
  const repeated = useMemo(() => {
    const seen = new Set()
    const twice = new Set()
    for (const session of props.sessions) (seen.has(session.title) ? twice : seen).add(session.title)
    return twice
  }, [props.sessions])
  const select = id => { props.onSelect(id); props.close() }
  const row = session => html`<${ConversationRow} key=${session.id} session=${session} selected=${props.activeId === session.id} showId=${repeated.has(session.title)}
    unread=${!!props.attention?.unread(session)} failed=${!!props.attention?.failed(session)}
    childCount=${tree.childCount.get(session.id) || 0} onSelect=${() => select(session.id)}
    onDelete=${() => props.onDelete(session.id)} onAskDelete=${() => setDeleting({ session, children: tree.childCount.get(session.id) || 0 })}
    onRename=${props.onRename} onMeta=${props.onMeta} onMoved=${setMoved} onTag=${addTag} onError=${props.onError} />`
  // A row, and while nothing is filtered, its subagent chats behind a toggle.
  const entry = session => {
    const kids = view.filtering ? [] : tree.children.get(session.id) || []
    if (!kids.length) return row(session)
    const open = openChildren[session.id] ?? kids.some(kid => kid.status !== 'idle')
    return html`<div class="conversation-entry" key=${session.id}>${row(session)}
      <button class="subagent-toggle" aria-expanded=${open} onClick=${() => setOpenChildren(state => ({ ...state, [session.id]: !open }))}>
        ${open ? html`<${ChevronDown} size=${12} />Hide subagents` : html`<${ChevronRight} size=${12} />Show ${plural(kids.length, 'subagent')}`}</button>
      ${open ? html`<div class="subagent-rows">${kids.map(kid => row(kid))}</div>` : null}
    </div>`
  }
  const rows = (key, sessions) => {
    const limit = limits[key] || ROWS_PER_GROUP
    return html`${sessions.slice(0, limit).map(entry)}
      ${sessions.length > limit ? html`<button class="show-more" onClick=${() => setLimits(state => ({ ...state, [key]: sessions.length }))}>
        Show ${sessions.length - limit} more</button>` : null}`
  }
  const group = ([key, sessions]) => {
    const open = view.filtering || (expanded[key] ?? (key === current || sessions.some(session => session.id === props.activeId)))
    const notes = open ? [] : badges(sessions, tree.children)
    return html`<section class="project-group" key=${`project:${key}`}>
      <button class="project-group-toggle" aria-expanded=${open} title=${key}
        onClick=${() => setExpanded(state => ({ ...state, [key]: !open }))}>
        ${open ? html`<${ChevronDown} size=${13} />` : html`<${ChevronRight} size=${13} />`}<span class="project-group-name">${folderName(key)}</span>
        <span class="project-group-count">${sessions.length}</span>
        ${notes.map(note => html`<span key=${note} class="row-badge project-badge">${note}</span>`)}
      </button>
      ${open ? rows(`project:${key}`, sessions) : null}
    </section>`
  }

  const searching = text.length >= SEARCH_MIN_CHARS
  const empty = props.sessions.length === 0 ? 'Your conversations will appear here.'
    : view.listed === 0 && runningOnly ? 'No conversation is running.'
      : view.listed === 0 && view.filtering && !view.archived.length && !(searching && messageHits(found, view.tagged).length)
        ? (terms.length ? 'No titles match.' : 'No conversation matches.') : ''
  return html`
    ${props.open ? html`<button class="sidebar-scrim" aria-label="Close sidebar" onClick=${props.close} />` : null}
    <aside class=${`sidebar ${props.open ? 'visible' : ''}`} ref=${aside}>
      <div class="brand"><${Brand} /><div><span>Local</span></div><span class="code-label"><${Code2} size=${17} />Code</span>
        <button class="icon-button mobile-only" onClick=${props.close} aria-label="Close sidebar"><${X} /></button>
      </div>
      <button class="new-conversation outline" aria-label="New conversation" onClick=${() => { props.onNew(); props.close() }}><${Plus} />New</button>
      <button class="project-button" onClick=${props.onSettings} title=${props.settings?.workspace}>
        <${Folder} /><span>${folder}</span><${ArrowLeftRight} size=${15} />
      </button>
      <div class="conversation-list">
        <div class="conversation-list-heading"><p class="section-label">Conversations</p>
          <${TagFilter} sessions=${props.sessions} selected=${tags} onChange=${setTags} />
          ${props.sessions.length ? html`<button class="running-filter" aria-pressed=${runningOnly} title="Show only conversations that are working or waiting"
            onClick=${() => setRunningOnly(value => !value)}>Running</button>` : null}</div>
        ${props.sessions.length ? html`<div class="conversation-search"><${Search} size=${14} />
          <input type="search" aria-label="Search conversations" placeholder="Search conversations or #tag" spellcheck=${false} maxLength=${200}
            value=${query} onInput=${event => setQuery(event.currentTarget.value)}
            onKeyDown=${event => { if (event.key === 'Escape' && query) { event.preventDefault(); setQuery('') } }} /></div>` : null}
        ${tags.length ? html`<div class="tag-filter"><span>${tags.length === 1 ? 'Tag' : 'Tags'}: ${tags.join(', ')}</span><button class="icon-button" aria-label="Clear tag filter"
          onClick=${() => { setTags([]); aside.current?.querySelector('.conversation-search input')?.focus() }}><${X} size=${12} /></button></div>` : null}
        ${empty ? html`<p class="empty-sessions">${empty}</p>` : null}
        ${view.pinned.length ? html`<section class="pinned-group" aria-label="Pinned"><p class="section-label"><${Pin} size=${12} />Pinned</p>
          ${rows('pinned', view.pinned)}</section>` : null}
        ${view.groups.map(group)}
        <${FoundInMessages} found=${searching ? found : null} tagged=${view.tagged} onOpen=${select} />
        ${view.archived.length ? html`<button class="archived-toggle" aria-expanded=${showArchived}
          onClick=${() => setShowArchived(value => !value)}>${showArchived ? html`<${ChevronDown} size=${13} />` : html`<${ChevronRight} size=${13} />`}Archived (${view.archived.length})</button>
          ${showArchived ? html`<section class="archived-group" aria-label="Archived">${rows('archived', view.archived)}</section>` : null}` : null}
      </div>
      <footer class="sidebar-footer">
        <button class="settings-button" onClick=${props.onSettings}><${SettingsIcon} />Settings</button>
        <${NotifyToggle} notifications=${props.attention?.notifications} />
        ${props.online === false ? html`<div class="connection-state" title="Reconnecting to your local server…">
          <span class="status-dot disconnected" />Local server offline</div>`
        : html`<div class="connection-state" title=${props.connection?.error || props.settings?.host || 'Checking connection'}>
          <span class=${`status-dot ${props.connection?.connected ? 'connected' : props.connection ? 'disconnected' : ''}`} />
          ${props.connection === null ? 'Checking Databricks…' : props.connection.connected ? 'Databricks connected' : 'Connection needs attention'}
        </div>`}
      </footer>
    </aside>
    ${deleting ? html`<${DeleteConversationDialog} session=${deleting.session} children=${deleting.children}
      onDelete=${mode => props.onDelete(deleting.session.id, mode === 'delete' ? 'delete' : undefined)} onClose=${() => setDeleting(null)} />` : null}`
}
