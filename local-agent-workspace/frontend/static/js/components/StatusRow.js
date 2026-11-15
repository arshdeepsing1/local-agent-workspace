import { html, useEffect, useLayoutEffect, useRef, useState } from '../lib.js'
import ChangesBar, { plural } from './ChangesBar.js'
import TaskChecklist, { taskProgress } from './TaskChecklist.js'
import ContinueButton, { CONTINUE_MESSAGE, stopReason } from './ContinueButton.js'
import QueueBar from './QueueBar.js'
import SideAnswer from './SideAnswer.js'
import CommandHelp from './CommandHelp.js'
import { RewindNotice } from '../rewindNotice.js'

// The one status row above the message box. It replaces the stack of bars that used to pile up there, and it never
// asks for more clicks than that stack did.
//
// What shows, and where:
//   errors   the text of an error (a queue that could not start, a failed check of the changed files) always shows in an
//            alert, whatever else is in the row: in its bar while the bar is shown, otherwise in an alert at the top.
//   primary  ONE of three things, in the bar's own form (so a single item looks and works as it did), by priority:
//              1. Continue with the queued messages. After a stop they always occur together, so they show together:
//                 Continue, then the queue with its Edit, Remove and Send queued messages buttons.
//              2. the changes
//              3. the tasks
//   lines    the /btw answer, the commands help and the rewind notice show at once, each on its own line, with their
//            buttons. They never turn Continue or the queue into a chip.
//   chips    each item that is not primary. A chip carries its main action as a compact button beside it: the changes
//            chip has Keep all and Undo all, the Continue chip has Continue, the queue chip has Send, so that action
//            needs no more clicks than before. Clicking the chip itself brings its item in as the primary one (a list
//            is open) and the item that was primary becomes a chip. The tasks chip is the toggle itself.
//
// A pick is forgotten as soon as its item goes away. A new item that outranks the picked one takes the lead.
//
// Keyboard focus and the pointer are never taken from an item. While focus is anywhere in the row, or the pointer is over
// an open list, an item that arrives (Continue after a stop, a message, changes) waits as a chip instead of replacing what
// you are using. It is announced in a polite live region ("Stopped. Continue is available.") and takes its place when focus
// and pointer leave. A chip's own button works at once, and a chip click brings its item in.
//
// Keyboard: everything is an ordinary button in Tab order, and every chip has an accessible name ("Show Tasks 0/3 done").
// Escape inside the row collapses what a chip opened, or an open list, and gives the lead back to the first item by
// priority. The row never changes height while you type: its items change only when the conversation does.
export const PRIORITY = ['continue', 'queue', 'changes', 'tasks', 'side', 'rewind', 'help']
const SHOWN = ['continue', 'queue', 'changes', 'tasks']  // what can be primary or a chip
const UNITS = ['urgent', 'changes', 'tasks']  // what can lead, most urgent first
const UNIT_OF = { continue: 'urgent', queue: 'urgent', changes: 'changes', tasks: 'tasks' }
const MEMBERS = { urgent: ['continue', 'queue'], changes: ['changes'], tasks: ['tasks'] }
const FIRST_CONTROL = ':is(button, [href], input, select, textarea, [tabindex]):not([disabled])'

// The text an error shows in the row (the bars hide their own while they sit here).
function errorsOf({ session, changes }) {
  return {
    queue: session?.queue_error || '',
    changes: changes?.error ? `Could not check changed files: ${changes.error}` : '',
  }
}

// A chip's visible text and its accessible name (which contains the visible text).
function chipOf(id, { session, changes, tasks }, error) {
  if (id === 'continue') return { text: 'Stopped', name: 'Show stopped reply' }
  if (id === 'queue') {
    const count = session?.queued_messages?.length || 0
    if (error) return { text: 'Queue error', name: 'Show queue error' }
    return { text: `${count} queued`, name: `Show ${plural(count, 'queued message')}` }
  }
  if (id === 'changes') {
    const count = changes?.files?.length || 0
    if (error) return { text: 'Changes error', name: 'Show changes error' }
    return { text: `${count} ${count === 1 ? 'change' : 'changes'}`, name: `Show ${count} ${count === 1 ? 'change' : 'changes'}` }
  }
  const { done, counted } = taskProgress(tasks)
  return { text: `Tasks ${done}/${counted}`, name: `Show Tasks ${done}/${counted} done` }
}

// What a screen reader hears when an item arrives.
function announce(id, { session, changes, tasks }, error) {
  if (id === 'continue') return 'Stopped. Continue is available.'
  if (id === 'queue') return error ? 'Queue error.' : `${plural(session?.queued_messages?.length || 0, 'message')} queued.`
  if (id === 'changes') return error ? 'Changes error.' : `${plural(changes?.files?.length || 0, 'file')} changed.`
  const { done, counted } = taskProgress(tasks)
  return `Tasks ${done}/${counted} done.`
}

// Keep all and Undo all beside the changes chip: the same two actions as the bar's, one click away.
function ChangesActions({ changes, busy, onAction, onError }) {
  const [working, setWorking] = useState(false)
  const files = changes?.files || []
  if (!files.length) return null
  const undoable = files.filter(item => item.current_hash)
  const run = async (action, items) => {
    if (!items.length) return
    setWorking(true)
    try { await onAction(action, items.map(item => ({ path: item.path, current_hash: item.current_hash }))) }
    catch (e) { onError(e.message) }
    finally { setWorking(false) }
  }
  const undoTitle = busy ? 'Stop the response before undoing changes.' : 'Restore these files to how they were before the agent changed them'
  return html`<button type="button" class="status-chip-action primary" aria-label="Keep all changes" title="Accept the changes to every file"
      disabled=${working} onClick=${() => void run('keep', files)}>Keep all</button>
    <button type="button" class="status-chip-action outline" aria-label="Undo all changes" title=${undoTitle}
      disabled=${working || busy || !undoable.length} onClick=${() => void run('undo', undoable)}>Undo all</button>`
}

// Props: session, sessionId (the open chat), viewKey (the parent's key for this view: the row starts afresh in another chat), changes, roots,
// busy, tasks, side (the /btw answer), notice (/help or /copy), canContinue, onSend(text), onError(message),
// onReview(path), onChanges(action, files), onRemoveQueued(id), onSendQueued(), onEditQueued(item), onDismissSide(),
// onDismissNotice(), onSelectSession(id).
export default function StatusRow(props) {
  const { session, changes, tasks, side, notice } = props
  const [picked, setPicked] = useState(null)  // a unit of UNITS
  const [epoch, setEpoch] = useState(0)
  const [focusIn, setFocusIn] = useState(false)
  const [hovered, setHovered] = useState(false)
  const [announcement, setAnnouncement] = useState('')
  const [working, setWorking] = useState(false)
  const row = useRef(null)
  const focusTarget = useRef(null)
  const deferred = useRef(new Set())  // arrived while the row was in use: shown as chips until it is free
  const before = useRef(null)  // what the last render showed
  const outranked = useRef(false)  // an item that waits outranks the picked one
  const errors = errorsOf(props)
  // What has something to show. An error alone is only the alert at the top: it needs no bar and no chip.
  const exists = {
    continue: !!stopReason(session),
    queue: !!(session?.queued_messages?.length || session?.queue_error),  // its bar shows its own error
    changes: !!changes?.files?.length,
    tasks: !!tasks?.length,
  }
  const present = SHOWN.filter(id => exists[id])
  const held = focusIn || hovered
  // An item that appears while the row is in use waits as a chip. Done while rendering, so it is never shown for a frame.
  if (before.current && held) {
    present.forEach(id => {
      if (before.current.has(id)) return
      deferred.current.add(id)
      if (picked && UNITS.indexOf(UNIT_OF[id]) < UNITS.indexOf(picked)) outranked.current = true
    })
  }
  if (!held) deferred.current.clear()
  deferred.current.forEach(id => { if (!exists[id]) deferred.current.delete(id) })
  const full = id => exists[id] && !deferred.current.has(id)
  const ready = UNITS.filter(unit => MEMBERS[unit].some(full))
  const lead = picked && ready.includes(picked) ? picked : ready[0] || null
  const shown = lead ? MEMBERS[lead].filter(full) : []
  const chips = SHOWN.filter(id => exists[id] && !shown.includes(id))
  const presentKey = present.join(' ')
  // Who has focus: asked again after every render, because a focused control that went away fires no event.
  useLayoutEffect(() => {
    const inside = !!row.current?.contains(document.activeElement)
    if (inside !== focusIn) setFocusIn(inside)
  })
  useEffect(() => {
    const last = before.current
    before.current = new Set(present)
    if (!last) return
    const fresh = present.filter(id => !last.has(id))
    if (fresh.length) setAnnouncement(fresh.map(id => announce(id, props, errors[id])).join(' '))
    // A new item that outranks the one you picked takes the lead, unless the row is in use.
    if (picked && !held && fresh.some(id => UNITS.indexOf(UNIT_OF[id]) < UNITS.indexOf(picked))) setPicked(null)
  }, [presentKey])
  // When the row is free, an item that waited and outranks the picked one takes the lead.
  useEffect(() => { if (!held && outranked.current) { outranked.current = false; setPicked(null) } }, [held])
  // A pick is forgotten when its item goes away, so it cannot open the list by itself when the item comes back.
  useEffect(() => { if (picked && !MEMBERS[picked].some(id => exists[id])) setPicked(null) })
  // After a chip or Escape the control that had focus is gone: focus goes to the first control of the item that opened.
  useEffect(() => {
    const target = focusTarget.current
    if (!target) return
    focusTarget.current = null
    const slot = row.current?.querySelector(`.status-slot[data-slot="${target}"]`) || row.current?.querySelector('.status-slot')
    slot?.querySelector(FIRST_CONTROL)?.focus()
  }, [picked, epoch])
  const pick = id => {
    focusTarget.current = id
    deferred.current.delete(id)
    setPicked(UNIT_OF[id])
    setEpoch(value => value + 1)
  }
  const collapse = event => {
    if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return
    if (picked === null && !row.current?.querySelector('.status-primary [aria-expanded="true"]')) return
    event.preventDefault()
    focusTarget.current = 'first'
    setPicked(null)
    setEpoch(value => value + 1)
  }
  const sendContinue = async () => {
    setWorking(true)
    try {
      await props.onSend(CONTINUE_MESSAGE)
      document.querySelector('textarea[aria-label="Message"]')?.focus()
    } catch (e) { props.onError(e.message) } finally { setWorking(false) }
  }
  const sendQueued = async () => {
    setWorking(true)
    try { await props.onSendQueued(); document.querySelector('textarea[aria-label="Message"]')?.focus() }
    catch (e) { props.onError(e.message) } finally { setWorking(false) }
  }
  const open = id => picked === id
  const key = id => `${id}-${props.viewKey}-${epoch}-${open(id) ? 'open' : 'first'}`
  const bars = {
    continue: () => html`<${ContinueButton} session=${session} disabled=${!props.canContinue} onSend=${props.onSend} onError=${props.onError} />`,
    queue: () => html`<${QueueBar} session=${session} onRemove=${props.onRemoveQueued} onSend=${props.onSendQueued}
      onEdit=${props.onEditQueued} onError=${props.onError} />`,
    changes: () => html`<${ChangesBar} changes=${changes} roots=${props.roots} busy=${props.busy} hideError initialOpen=${open('changes')}
      onOpen=${props.onReview} onAction=${props.onChanges} onError=${props.onError} />`,
    tasks: () => html`<${TaskChecklist} tasks=${tasks} initialOpen=${open('tasks')} />`,
  }
  // A new key starts an opened or collapsed list afresh; Continue and the queue keep theirs.
  const slot = id => html`<div class="status-slot" data-slot=${id} key=${MEMBERS.urgent.includes(id) ? id : key(id)}>${bars[id]()}</div>`
  const actions = id => {
    if (id === 'continue') return html`<button type="button" class="status-chip-action outline" title=${`Send “${CONTINUE_MESSAGE}”`}
      disabled=${!props.canContinue || working} onClick=${() => void sendContinue()}>Continue</button>`
    if (id === 'queue') return session?.status === 'idle' && session?.queued_messages?.length
      ? html`<button type="button" class="status-chip-action outline" aria-label="Send queued messages" disabled=${working}
        onClick=${() => void sendQueued()}>Send</button>` : null
    if (id === 'changes') return html`<${ChangesActions} changes=${changes} busy=${props.busy} onAction=${props.onChanges} onError=${props.onError} />`
    return null
  }
  return html`<div class="status-row" role="group" aria-label="Conversation status" ref=${row} onKeyDown=${collapse}
      onFocusCapture=${() => setFocusIn(true)} onBlurCapture=${event => { if (!row.current?.contains(event.relatedTarget)) setFocusIn(false) }}
      onMouseOver=${event => setHovered(!!event.target.closest?.('.changes-list, .task-checklist-list'))} onMouseLeave=${() => setHovered(false)}>
    <div class="status-errors">
      ${errors.queue && !shown.includes('queue') ? html`<p class="status-error" role="alert" key="queue">${errors.queue}</p>` : null}
      ${errors.changes ? html`<p class="status-error" role="alert" key="changes">${errors.changes}</p>` : null}
    </div>
    <div class="status-primary" data-item=${shown[0] || null}>
      ${shown.map(slot)}
    </div>
    <div class="status-lines">
      ${side ? html`<div class="status-line" key="side"><${SideAnswer} side=${side} onDismiss=${props.onDismissSide} onError=${props.onError} /></div>` : null}
      ${notice ? html`<div class="status-line" key="help"><${CommandHelp} notice=${notice} onDismiss=${props.onDismissNotice} /></div>` : null}
    </div>
    <div class="status-rewind" aria-live="polite"><${RewindNotice} sessionId=${props.sessionId} onSelectSession=${props.onSelectSession} /></div>
    ${chips.length ? html`<div class="status-chips">${chips.map(id => {
      const chip = chipOf(id, props, errors[id])
      return html`<span class="status-chip-group" key=${id}>
        <button type="button" class="status-chip" aria-label=${chip.name} title=${chip.name}
          aria-expanded=${id === 'changes' || id === 'tasks' ? 'false' : null} onClick=${() => pick(id)}>${chip.text}</button>${actions(id)}</span>`
    })}</div>` : null}
  </div>
  <div class="status-live sr-only" aria-live="polite" aria-atomic="true">${announcement}</div>`
}
