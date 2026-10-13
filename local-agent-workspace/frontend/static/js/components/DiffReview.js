import { html, useEffect, useMemo, useRef, useState } from '../lib.js'
import { ChevronDown, ChevronUp, MessageSquarePlus, X } from '../icons.js'
import { api } from '../api.js'
import { MAX_COMMENT_LENGTH, MAX_COMMENTS, MAX_MESSAGE_LENGTH, TOO_MANY, reviewMessage, useReviewComments } from '../reviewComments.js'
import { DiffStat, STATUS_LABELS } from './ChangesBar.js'
import { blockerOf, OpenBlockingChat } from './BlockingChat.js'

// The agent's changes to one file, inline: removed lines in red, added lines in
// green, Keep/Undo on each change, and a bar at the bottom right that moves between
// changes and keeps or undoes the whole file.
// Diff: { path, status, added, removed, baseline_hash, current_hash,
//   hunks: [{ index, line, added, removed }],
//   lines: [{ kind: 'context'|'removed'|'added', text, old?, new?, hunk?, newline? }] }
// Props: sessionId, path, version (changes when the file or its review changes),
//   busy (a response is running; Undo waits for it), onAction(action, files) -> Promise,
//   onSelectSession(id) (opens a chat that an error names as blocking Undo),
//   onSendComments(message) -> Promise (sends the line comments to the agent as a message)
// One line of a diff: { kind: 'context'|'removed'|'added'|'hunk', text, old?, new?, newline? }.
// A 'hunk' line is a Git hunk header ('@@ -1,3 +1,4 @@'), shown as a separator.
// action: an element shown before the line numbers (the review's comment button).
export function DiffLine({ line, action }) {
  if (line.kind === 'hunk') return html`<div class="diff-line hunk"><span class="line-text">${line.text}</span></div>`
  return html`<div class=${`diff-line ${line.kind}`}>${action || null}
    <span class="line-number">${line.old ?? ''}</span><span class="line-number">${line.new ?? ''}</span>
    <span class="line-marker" aria-hidden="true">${line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ''}</span>
    <span class="line-text"><span class="sr-only">${line.kind === 'added' ? 'Added: ' : line.kind === 'removed' ? 'Removed: ' : ''}</span>${line.text}${line.newline === false ? html`<small class="no-newline">No newline at end of file</small>` : null}</span>
  </div>`
}

// Where a comment on a diff line points: removed lines by their old number, others by their new one.
const lineRef = line => line.kind === 'removed' ? { kind: 'removed', line: line.old } : { kind: line.kind, line: line.new ?? line.old }
const sameLine = (comment, ref) => comment.line === ref.line && (comment.kind === 'removed') === (ref.kind === 'removed')

// One comment under its line, with its line reference and a button to remove it.
function LineComment({ item, onRemove }) {
  return html`<div class="review-comment">
    <span class="review-comment-line">${item.kind === 'removed' ? `Removed line ${item.line}` : `Line ${item.line}`}</span>
    <span class="review-comment-text">${item.comment}</span>
    <button class="icon-button" aria-label="Remove comment" title="Remove comment" onClick=${onRemove}><${X} size=${13} /></button>
  </div>`
}

export default function DiffReview({ sessionId, path, version, busy, onAction, onSelectSession, onSendComments }) {
  const [diff, setDiff] = useState(null)
  const [loadError, setLoadError] = useState('')
  const [error, setError] = useState('')  // The last Keep/Undo failure; a refresh keeps it visible.
  const [blocker, setBlocker] = useState(null)  // The other chat that error names, if any.
  const [active, setActive] = useState(0)
  const [working, setWorking] = useState(false)
  const [revision, setRevision] = useState(0)
  const scroller = useRef(null)
  const review = useReviewComments(sessionId)
  const [composing, setComposing] = useState(null)  // { kind, line, text, draft }: the open comment form.
  const [sending, setSending] = useState(false)
  const commentBox = useRef(null)
  useEffect(() => { if (composing) commentBox.current?.focus() }, [composing?.kind, composing?.line])
  const fileComments = review.comments.filter(item => item.path === path)
  const addComment = () => {
    const text = composing.draft.trim()
    if (!text) return
    if (review.comments.length >= MAX_COMMENTS) { setError(TOO_MANY); return }
    review.add({ path, line: composing.line, kind: composing.kind, text: composing.text, comment: text })
    setComposing(null)
  }
  const sendComments = async () => {
    const sent = review.comments
    const message = reviewMessage(sent)
    if (sent.length > MAX_COMMENTS || message.length > MAX_MESSAGE_LENGTH) { setError(TOO_MANY); return }
    setSending(true); setError(''); setBlocker(null)
    try { await onSendComments(message); review.clear(sent.map(item => item.id)) }
    catch (e) { setError(e.message) }
    finally { setSending(false) }
  }
  const count = review.comments.length
  useEffect(() => {
    let current = true
    api(`/sessions/${encodeURIComponent(sessionId)}/changes/diff?path=${encodeURIComponent(path)}`)
      .then(result => {
        if (!current) return
        setDiff(result); setLoadError('')
        setActive(index => Math.min(index, Math.max(result.hunks.length - 1, 0)))
      })
      .catch(e => { if (current) setLoadError(e.message) })
    return () => { current = false }
  }, [sessionId, path, version, revision])
  const starts = useMemo(() => new Map((diff?.hunks || []).map(hunk => [hunk.line, hunk.index])), [diff])
  const changes = diff?.hunks.length || 0
  const go = index => {
    setActive(index)
    scroller.current?.querySelector(`[data-hunk="${index}"]`)?.scrollIntoView({ block: 'center' })
  }
  const run = async (action, hunk) => {
    if (!diff || working) return
    setWorking(true); setError(''); setBlocker(null)
    const file = { path, current_hash: diff.current_hash }
    try { await onAction(action, [hunk === undefined ? file : { ...file, baseline_hash: diff.baseline_hash, hunk }]) }
    catch (e) { setError(e.message); setBlocker(blockerOf(e)) }
    finally { setWorking(false); setRevision(value => value + 1) }
  }
  const undoTitle = busy ? 'Stop the response before undoing changes.' : undefined
  const commentButton = line => {
    const ref = lineRef(line)
    const label = line.kind === 'removed' ? `Comment on removed line ${ref.line}` : `Comment on line ${ref.line}`
    return html`<button class="line-comment" aria-label=${label} title=${label}
      onClick=${() => setComposing({ ...ref, text: line.text, draft: '' })}><${MessageSquarePlus} size=${13} /></button>`
  }
  const commentForm = composing ? html`<div class="review-comment-form">
    <textarea aria-label="Comment" rows=${3} maxLength=${MAX_COMMENT_LENGTH} spellcheck=${false} value=${composing.draft}
      ref=${commentBox}
      placeholder="Ask the agent for a change on this line"
      onInput=${e => { const draft = e.currentTarget.value; setComposing(current => current && { ...current, draft }) }}
      onKeyDown=${e => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); addComment() }
        else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setComposing(null) }
      }} />
    <div><button type="button" class="outline" onClick=${() => setComposing(null)}>Cancel</button>
      <button type="button" class="primary" disabled=${!composing.draft.trim()} onClick=${addComment}>Add comment</button></div>
  </div>` : null
  const orphans = diff ? fileComments.filter(item => !diff.lines.some(line => line.kind !== 'hunk' && sameLine(item, lineRef(line)))) : []
  if (!diff) return html`<div class="diff-review">${loadError ? html`<div class="panel-error" role="alert">${loadError}</div>` : html`<p class="panel-empty">Loading changes…</p>`}</div>`
  return html`<div class="diff-review" role="region" aria-label=${`Agent changes in ${path}`}>
    <div class="diff-review-summary"><span>${STATUS_LABELS[diff.status] || diff.status}</span><${DiffStat} added=${diff.added} removed=${diff.removed} />
      <span class="diff-review-note">${changes ? `${changes} ${changes === 1 ? 'change' : 'changes'} by the agent` : diff.status === 'created' ? 'Empty new file' : 'No line changes'}</span></div>
    ${error || loadError ? html`<div class=${`panel-error ${error && blocker ? 'blocked-error' : ''}`} role="alert">${error ? html`<span>${error}</span>` : loadError}
      <${OpenBlockingChat} blocker=${error ? blocker : null} currentId=${sessionId} onSelect=${onSelectSession} /></div>` : null}
    <div class="diff-review-lines" ref=${scroller} tabindex="0">
      ${diff.lines.map((line, index) => {
        const hunk = starts.get(index)
        return html`${hunk === undefined ? null : html`<div key=${`hunk-${hunk}`} class=${`hunk-actions ${hunk === active ? 'active' : ''}`} data-hunk=${hunk}>
            <span>Change ${hunk + 1} of ${changes}</span>
            <button aria-label=${`Keep change ${hunk + 1}`} disabled=${working} onClick=${() => { setActive(hunk); void run('keep', hunk) }}>Keep</button>
            <button aria-label=${`Undo change ${hunk + 1}`} title=${undoTitle} disabled=${working || busy} onClick=${() => { setActive(hunk); void run('undo', hunk) }}>Undo</button>
          </div>`}
          <${DiffLine} key=${index} line=${line} action=${commentButton(line)} />
          ${fileComments.filter(item => sameLine(item, lineRef(line))).map(item => html`<${LineComment} key=${item.id} item=${item} onRemove=${() => review.remove(item.id)} />`)}
          ${composing && sameLine(composing, lineRef(line)) ? commentForm : null}`
      })}
      ${orphans.length ? html`<div class="review-comment-orphans"><span>Comments on lines no longer in this diff</span>
        ${orphans.map(item => html`<${LineComment} key=${item.id} item=${item} onRemove=${() => review.remove(item.id)} />`)}</div>` : null}
    </div>
    <div class="review-bar" role="toolbar" aria-label="Review this file">
      <button class="icon-button" aria-label="Previous change" disabled=${changes < 2} onClick=${() => go((active - 1 + changes) % changes)}><${ChevronUp} size=${16} /></button>
      <span class="review-position">${changes ? `${active + 1} of ${changes}` : '0 of 0'}</span>
      <button class="icon-button" aria-label="Next change" disabled=${changes < 2} onClick=${() => go((active + 1) % changes)}><${ChevronDown} size=${16} /></button>
      <button class="primary" aria-label="Keep this file" title="Accept every change in this file" disabled=${working} onClick=${() => void run('keep')}>Keep</button>
      <button class="outline" aria-label="Undo this file" title=${undoTitle || 'Restore this file to how it was before the agent changed it'} disabled=${working || busy} onClick=${() => void run('undo')}>Undo</button>
      ${count ? html`<button class="outline review-send" aria-label=${`Send ${count} review ${count === 1 ? 'comment' : 'comments'}`}
        title=${busy ? 'Wait for the reply to finish' : 'Send every comment in this conversation to the agent as one message'}
        disabled=${busy || sending || !onSendComments} onClick=${() => void sendComments()}>Send ${count} ${count === 1 ? 'comment' : 'comments'}</button>` : null}
    </div>
  </div>`
}
