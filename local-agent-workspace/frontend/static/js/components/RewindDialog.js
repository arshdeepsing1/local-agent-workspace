import { html, hiddenNote, noteLine, visibleParts, useEffect, useRef, useState } from '../lib.js'
import { api } from '../api.js'
import DialogShell from './DialogShell.js'
import { RefusedFiles } from './EditMessage.js'
import { blockerOf, OpenBlockingChat } from './BlockingChat.js'

const FILE_STATUS = {
  restore: 'Will be restored',
  unchanged: 'Already as it was',
  drifted: 'Changed since the agent\'s last edit',
  interleaved: 'Also changed by something else between the agent\'s edits',
}

const CHOICES = [['both', 'Conversation and code'], ['conversation', 'Conversation only'], ['code', 'Code only']]

// The user messages and the replies from this message on (what a conversation rewind removes).
export function removedCounts(events, eventId) {
  const start = events.findIndex(item => item.id === eventId)
  const removed = start < 0 ? [] : events.slice(start)
  return { messages: removed.filter(item => item.type === 'user').length,
    replies: removed.filter(item => item.type === 'assistant' && item.text).length }
}

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`

export function removedLine({ messages, replies }) {
  return `Removes ${plural(messages, 'message', 'messages')} and ${plural(replies, 'reply', 'replies')} after this point.`
}

// Rewind to a user message: the conversation (in place, after an archived copy), the agent's file
// edits, or both. Props: session, event (the user event), onRewound({ conversation, backupId }), onClose,
// onSelectSession(id) (opens a chat that blocks the file rewind).
export default function RewindDialog({ session, event, onRewound, onClose, onSelectSession }) {
  const dialog = useRef(null)
  const [preview, setPreview] = useState(null)
  const [choice, setChoice] = useState(null)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    let current = true
    api(`/sessions/${encodeURIComponent(session.id)}/rewind-preview?before_event_id=${encodeURIComponent(event.id)}`)
      .then(result => {
        if (!current) return
        setPreview(result)
        const conversation = result.conversation.possible
        const code = result.code.possible
        setChoice(conversation && code && result.code.files.length ? 'both' : conversation ? 'conversation' : code ? 'code' : null)
      })
      .catch(e => { if (current) setError(e) })
    return () => { current = false }
  }, [session.id, event.id])
  const possible = value => !!preview && (value === 'both' ? preview.conversation.possible && preview.code.possible
    : preview[value].possible)
  const submit = async e => {
    e.preventDefault()
    if (saving || !choice) return
    setSaving(true); setError(null)
    const conversation = choice !== 'code'
    try {
      const result = await api(`/sessions/${encodeURIComponent(session.id)}/rewind`, 'POST',
        { before_event_id: event.id, conversation, code: choice !== 'conversation' })
      // Closed first: focus returns to the opener, and onRewound can then move it out of the dialog.
      dialog.current?.close()
      onRewound({ conversation, backupId: result.backup_session_id })
    } catch (e) { setError(e); setSaving(false) }
  }
  const counts = removedCounts(session.events, event.id)
  return html`<${DialogShell} className="rewind-dialog" label="Rewind conversation" title="Rewind to this message"
    description=${html`<p class="rewind-quote">${(event.text || '').slice(0, 200)}</p>`}
    onClose=${onClose} onSubmit=${submit} dialogRef=${dialog} busy=${saving}
    footer=${html`<button type="button" class="outline" disabled=${saving} onClick=${onClose}>Cancel</button>
        <button type="submit" class="primary" disabled=${saving || !choice || !possible(choice)}>Rewind</button>`}>
      ${!preview && !error ? html`<p class="muted">Checking what can be rewound…</p>` : null}
      ${preview ? html`<fieldset><legend>Rewind</legend>${CHOICES.map(([value, label]) => {
        const reason = !possible(value) && value !== 'both' ? preview[value].reason : null
        return html`<label class="choice" key=${value}><input type="radio" name="rewind-choice" value=${value} checked=${choice === value}
          disabled=${saving || !possible(value)} onChange=${() => setChoice(value)} /><span>${label}${reason ? html`<small class="choice-reason">${reason}</small>` : null}</span></label>`
      })}</fieldset>
        ${preview.conversation.possible && choice !== 'code' ? html`<p class="field-help">${removedLine(counts)}</p>` : null}
        ${preview.conversation.summary_dropped && choice !== 'code' ? html`<p class="field-help">The summary of earlier turns covers messages after this point, so it is dropped; the next message may compact again.</p>` : null}
        ${preview.code.files.length ? html`<ul class="rewind-files" aria-label="Files">${preview.code.files.map(file => html`<li key=${file.path}>
          <span class="rewind-path logical-order">${visibleParts(file.path)}</span><span class=${`rewind-status ${file.status}`}>${file.status === 'unavailable' ? file.error || 'Unavailable' : FILE_STATUS[file.status] || file.status}</span></li>`)}</ul>
          ${noteLine(hiddenNote(preview.code.files.map(file => file.path)))}` : null}
        <p class="settings-note">Changes made by commands or by subagents are not rewound. The current version is kept as an archived conversation.</p>` : null}
      ${error ? html`<div class="form-error" role="alert"><p>${error.message}</p><${RefusedFiles} error=${error} />
        <${OpenBlockingChat} blocker=${blockerOf(error)} currentId=${session.id} onSelect=${onSelectSession} /></div>` : null}
  <//>`
}
